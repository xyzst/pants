// Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{self, AtomicU32};
use std::sync::{Arc, Weak};
use std::time::{Duration, Instant};

use crate::context::Core;
use crate::nodes::{NodeKey, Select};
use crate::python::{Failure, Value};

use async_latch::AsyncLatch;
use futures::future::{self, AbortHandle, Abortable};
use futures::FutureExt;
use graph::LastObserved;
use log::warn;
use parking_lot::{Mutex, RwLock};
use pyo3::prelude::*;
use task_executor::Executor;
use tokio::signal::unix::{signal, SignalKind};
use ui::ConsoleUI;
use workunit_store::{format_workunit_duration_ms, RunId, UserMetadataPyValue, WorkunitStore};

// When enabled, the interval at which all stragglers that have been running for longer than a
// threshold should be logged. The threshold might become configurable, but this might not need
// to be.
const STRAGGLER_LOGGING_INTERVAL: Duration = Duration::from_secs(30);

// Root requests are limited to Select nodes, which produce (python) Values.
pub type Root = Select;

pub type ObservedValueResult = Result<(Value, Option<LastObserved>), Failure>;

///
/// An enum for the two cases of `--[no-]dynamic-ui`.
///
enum SessionDisplay {
  // The dynamic UI is enabled, and the ConsoleUI should interact with a TTY.
  ConsoleUI(ConsoleUI),
  // The dynamic UI is disabled, and we should use only logging.
  Logging {
    straggler_threshold: Duration,
    straggler_deadline: Option<Instant>,
  },
}

impl SessionDisplay {
  fn new(
    workunit_store: &WorkunitStore,
    parallelism: usize,
    should_render_ui: bool,
  ) -> SessionDisplay {
    if should_render_ui {
      SessionDisplay::ConsoleUI(ConsoleUI::new(workunit_store.clone(), parallelism))
    } else {
      SessionDisplay::Logging {
        // TODO: This threshold should likely be configurable, but the interval we render at
        // probably does not need to be.
        straggler_threshold: Duration::from_secs(60),
        straggler_deadline: None,
      }
    }
  }
}

///
/// The portion of a Session that uniquely identifies it and holds metrics and the history of
/// requests made on it.
///
struct SessionState {
  // The Core that this Session is running on.
  core: Arc<Core>,
  // The total size of the graph at Session-creation time.
  preceding_graph_size: usize,
  // The set of roots that have been requested within this session, with associated LastObserved
  // times if they were polled.
  roots: Mutex<HashMap<Root, Option<LastObserved>>>,
  // A place to store info about workunits in rust part
  workunit_store: WorkunitStore,
  // Per-Session values that have been set for this session.
  session_values: Mutex<PyObject>,
  // An id used to control the visibility of uncacheable rules. Generally this is identical for an
  // entire Session, but in some cases (in particular, a `--loop`) the caller wants to retain the
  // same Session while still observing new values for uncacheable rules like Goals.
  run_id: AtomicU32,
  workunit_metadata_map: RwLock<HashMap<UserMetadataPyValue, PyObject>>,
}

///
/// A cancellable handle to a Session, with an optional associated UI.
///
struct SessionHandle {
  // The unique id for this Session: used for metrics gathering purposes.
  build_id: String,
  // Whether or not this Session has been cancelled. If a Session has been cancelled, all work that
  // it started should attempt to exit in an orderly fashion.
  cancelled: AsyncLatch,
  // True if this Session should be shielded from keyboard interrupts (which cancel all
  // non-isolated Sessions).
  isolated: bool,
  // The display mechanism to use in this Session.
  display: tokio::sync::Mutex<SessionDisplay>,
}

impl SessionHandle {
  ///
  /// Cancels this Session.
  ///
  pub fn cancel(&self) {
    self.cancelled.trigger();
  }
}

impl Drop for SessionHandle {
  fn drop(&mut self) {
    self.cancelled.trigger();
  }
}

///
/// A Session represents a related series of requests (generally: one run of the pants CLI) on an
/// underlying Scheduler, and is a useful scope for metrics.
///
/// Both Scheduler and Session are exposed to python and expected to be used by multiple threads, so
/// they use internal mutability in order to avoid exposing locks to callers.
///
/// NB: The `SessionState` and `SessionHandle` structs are independent in order to allow for a
/// shallow clone of a Session with independent cancellation and display properties, but which
/// shares the same metrics and identity.
///
#[derive(Clone)]
pub struct Session {
  handle: Arc<SessionHandle>,
  state: Arc<SessionState>,
}

impl Session {
  pub fn new(
    core: Arc<Core>,
    should_render_ui: bool,
    build_id: String,
    session_values: PyObject,
    cancelled: AsyncLatch,
  ) -> Result<Session, String> {
    let workunit_store = WorkunitStore::new(!should_render_ui);
    let display = tokio::sync::Mutex::new(SessionDisplay::new(
      &workunit_store,
      core.local_parallelism,
      should_render_ui,
    ));

    let handle = Arc::new(SessionHandle {
      build_id,
      cancelled,
      isolated: false,
      display,
    });
    core.sessions.add(&handle)?;
    let run_id = core.sessions.generate_run_id();
    let preceding_graph_size = core.graph.len();
    Ok(Session {
      handle,
      state: Arc::new(SessionState {
        core,
        preceding_graph_size,
        roots: Mutex::new(HashMap::new()),
        workunit_store,
        session_values: Mutex::new(session_values),
        run_id: AtomicU32::new(run_id.0),
        workunit_metadata_map: RwLock::new(HashMap::new()),
      }),
    })
  }

  ///
  /// Creates a shallow clone of this Session which is independently cancellable, but which shares
  /// metrics, identity, and state with the original.
  ///
  /// Useful when executing background work "on behalf of a Session" which should not be torn down
  /// when a client disconnects, or killed by Ctrl+C.
  ///
  pub fn isolated_shallow_clone(&self, build_id: String) -> Result<Session, String> {
    let display = tokio::sync::Mutex::new(SessionDisplay::new(
      &self.state.workunit_store,
      self.state.core.local_parallelism,
      false,
    ));
    let handle = Arc::new(SessionHandle {
      build_id,
      isolated: true,
      cancelled: AsyncLatch::new(),
      display,
    });
    self.state.core.sessions.add(&handle)?;
    Ok(Session {
      handle,
      state: self.state.clone(),
    })
  }

  pub fn core(&self) -> &Arc<Core> {
    &self.state.core
  }

  ///
  /// Cancels this Session.
  ///
  pub fn cancel(&self) {
    self.handle.cancel();
  }

  ///
  /// Returns true if this Session has been cancelled.
  ///
  pub fn is_cancelled(&self) -> bool {
    self.handle.cancelled.poll_triggered()
  }

  ///
  /// Returns only if this Session has been cancelled.
  ///
  pub async fn cancelled(&self) {
    self.handle.cancelled.triggered().await;
  }

  pub fn with_metadata_map<F, T>(&self, f: F) -> T
  where
    F: FnOnce(&mut HashMap<UserMetadataPyValue, PyObject>) -> T,
  {
    f(&mut self.state.workunit_metadata_map.write())
  }

  pub fn roots_extend(&self, new_roots: Vec<(Root, Option<LastObserved>)>) {
    let mut roots = self.state.roots.lock();
    roots.extend(new_roots);
  }

  pub fn roots_zip_last_observed(&self, inputs: &[Root]) -> Vec<(Root, Option<LastObserved>)> {
    let roots = self.state.roots.lock();
    inputs
      .iter()
      .map(|root| {
        let last_observed = roots.get(root).cloned().unwrap_or(None);
        (root.clone(), last_observed)
      })
      .collect()
  }

  pub fn roots_nodes(&self) -> Vec<NodeKey> {
    let roots = self.state.roots.lock();
    roots.keys().map(|r| r.clone().into()).collect()
  }

  pub fn session_values(&self) -> PyObject {
    self.state.session_values.lock().clone()
  }

  pub fn preceding_graph_size(&self) -> usize {
    self.state.preceding_graph_size
  }

  pub fn workunit_store(&self) -> WorkunitStore {
    self.state.workunit_store.clone()
  }

  pub fn build_id(&self) -> &String {
    &self.handle.build_id
  }

  pub fn run_id(&self) -> RunId {
    RunId(self.state.run_id.load(atomic::Ordering::SeqCst))
  }

  pub fn new_run_id(&self) {
    self.state.run_id.store(
      self.state.core.sessions.generate_run_id().0,
      atomic::Ordering::SeqCst,
    );
  }

  pub async fn with_console_ui_disabled<T>(&self, f: impl Future<Output = T>) -> T {
    match *self.handle.display.lock().await {
      SessionDisplay::ConsoleUI(ref mut ui) => ui.with_console_ui_disabled(f).await,
      SessionDisplay::Logging { .. } => f.await,
    }
  }

  pub async fn maybe_display_initialize(&self, executor: &Executor) {
    let result = match *self.handle.display.lock().await {
      SessionDisplay::ConsoleUI(ref mut ui) => ui.initialize(executor.clone()),
      SessionDisplay::Logging {
        ref mut straggler_deadline,
        ..
      } => {
        *straggler_deadline = Some(Instant::now() + STRAGGLER_LOGGING_INTERVAL);
        Ok(())
      }
    };
    if let Err(e) = result {
      warn!("{}", e);
    }
  }

  pub async fn maybe_display_teardown(&self) {
    let teardown = match *self.handle.display.lock().await {
      SessionDisplay::ConsoleUI(ref mut ui) => ui.teardown().boxed(),
      SessionDisplay::Logging {
        ref mut straggler_deadline,
        ..
      } => {
        *straggler_deadline = None;
        async { Ok(()) }.boxed()
      }
    };
    if let Err(e) = teardown.await {
      warn!("{}", e);
    }
  }

  pub fn maybe_display_render(&self) {
    let mut display = if let Ok(display) = self.handle.display.try_lock() {
      display
    } else {
      // Else, the UI is currently busy: skip rendering.
      return;
    };
    match *display {
      SessionDisplay::ConsoleUI(ref mut ui) => ui.render(),
      SessionDisplay::Logging {
        straggler_threshold,
        ref mut straggler_deadline,
      } => {
        if straggler_deadline
          .map(|sd| sd < Instant::now())
          .unwrap_or(false)
        {
          *straggler_deadline = Some(Instant::now() + STRAGGLER_LOGGING_INTERVAL);
          let straggling_workunits = self
            .state
            .workunit_store
            .straggling_workunits(straggler_threshold);
          if !straggling_workunits.is_empty() {
            log::info!(
              "Long running tasks:\n  {}",
              straggling_workunits
                .into_iter()
                .map(|(duration, desc)| format!(
                  "{}\t{}",
                  format_workunit_duration_ms!(duration.as_millis()),
                  desc
                ))
                .collect::<Vec<_>>()
                .join("\n  ")
            );
          }
        }
      }
    }
  }
}

///
/// A collection of all live Sessions.
///
/// The `Sessions` struct maintains a task monitoring SIGINT, and cancels all current Sessions each time
/// it arrives.
///
pub struct Sessions {
  /// Live sessions. Completed Sessions (i.e., those for which the Weak reference is dead) are
  /// removed from this collection on a best effort when new Sessions are created.
  ///
  /// If the wrapping Option is None, it is because `fn shutdown` is running, and the associated
  /// Core/Scheduler are being shut down.
  sessions: Arc<Mutex<Option<Vec<Weak<SessionHandle>>>>>,
  /// Handle to kill the signal monitoring task when this object is killed.
  signal_task_abort_handle: AbortHandle,
  /// A generator for RunId values. Although this is monotonic, there is no meaning assigned to
  /// ordering: only equality is relevant.
  run_id_generator: AtomicU32,
}

impl Sessions {
  pub fn new(executor: &Executor) -> Result<Sessions, String> {
    let sessions: Arc<Mutex<Option<Vec<Weak<SessionHandle>>>>> =
      Arc::new(Mutex::new(Some(Vec::new())));
    // A task that watches for keyboard interrupts arriving at this process, and cancels all
    // non-isolated Sessions.
    let signal_task_abort_handle = {
      let mut signal_stream = signal(SignalKind::interrupt())
        .map_err(|err| format!("Failed to install interrupt handler: {}", err))?;
      let (abort_handle, abort_registration) = AbortHandle::new_pair();
      let sessions = sessions.clone();
      let _ = executor.spawn(Abortable::new(
        async move {
          loop {
            let _ = signal_stream.recv().await;
            let cancellable_sessions = {
              let sessions = sessions.lock();
              if let Some(ref sessions) = *sessions {
                sessions
                  .iter()
                  .flat_map(|session| session.upgrade())
                  .filter(|session| !session.isolated)
                  .collect::<Vec<_>>()
              } else {
                vec![]
              }
            };
            for session in cancellable_sessions {
              session.cancel();
            }
          }
        },
        abort_registration,
      ));
      abort_handle
    };
    Ok(Sessions {
      sessions,
      signal_task_abort_handle,
      run_id_generator: AtomicU32::new(0),
    })
  }

  fn add(&self, handle: &Arc<SessionHandle>) -> Result<(), String> {
    let mut sessions = self.sessions.lock();
    if let Some(ref mut sessions) = *sessions {
      sessions.retain(|weak_handle| weak_handle.upgrade().is_some());
      sessions.push(Arc::downgrade(handle));
      Ok(())
    } else {
      Err("The scheduler is shutting down: no new sessions may be created.".to_string())
    }
  }

  fn generate_run_id(&self) -> RunId {
    RunId(self.run_id_generator.fetch_add(1, atomic::Ordering::SeqCst))
  }

  ///
  /// Shuts down this Sessions instance by waiting for all existing Sessions to exit.
  ///
  /// Waits at most `timeout` for Sessions to complete.
  ///
  pub async fn shutdown(&self, timeout: Duration) -> Result<(), String> {
    if let Some(sessions) = self.sessions.lock().take() {
      // Collect clones of the cancellation tokens for each Session, which allows us to watch for
      // them to have been dropped.
      let (build_ids, cancellation_latches): (Vec<_>, Vec<_>) = sessions
        .into_iter()
        .filter_map(|weak_handle| weak_handle.upgrade())
        .map(|handle| {
          let build_id = handle.build_id.clone();
          let cancelled = handle.cancelled.clone();
          let cancellation_triggered = async move {
            cancelled.triggered().await;
            log::info!("Shutdown completed: {:?}", build_id)
          };
          (handle.build_id.clone(), cancellation_triggered)
        })
        .unzip();

      if !build_ids.is_empty() {
        log::info!("Waiting for shutdown of: {:?}", build_ids);
        tokio::time::timeout(timeout, future::join_all(cancellation_latches))
          .await
          .map_err(|_| format!("Some Sessions did not shutdown within {:?}.", timeout))?;
      }
    }
    Ok(())
  }
}

impl Drop for Sessions {
  fn drop(&mut self) {
    self.signal_task_abort_handle.abort();
  }
}
