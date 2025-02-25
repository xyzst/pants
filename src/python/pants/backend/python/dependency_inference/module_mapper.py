# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import enum
import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePath
from typing import DefaultDict, Iterable, Tuple

from packaging.utils import canonicalize_name as canonicalize_project_name

from pants.backend.python.dependency_inference.default_module_mapping import (
    DEFAULT_MODULE_MAPPING,
    DEFAULT_TYPE_STUB_MODULE_MAPPING,
)
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import (
    PythonRequirementCompatibleResolvesField,
    PythonRequirementModulesField,
    PythonRequirementsField,
    PythonRequirementTypeStubModulesField,
    PythonSourceField,
)
from pants.core.util_rules.stripped_source_files import StrippedFileName, StrippedFileNameRequest
from pants.engine.addresses import Address
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import AllTargets, Target
from pants.engine.unions import UnionMembership, UnionRule, union
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


class ModuleProviderType(enum.Enum):
    TYPE_STUB = enum.auto()
    IMPL = enum.auto()


@dataclass(frozen=True, order=True)
class ModuleProvider:
    addr: Address
    typ: ModuleProviderType


@dataclass(frozen=True)
class PythonModule:
    module: str

    @classmethod
    def create_from_stripped_path(cls, path: PurePath) -> PythonModule:
        module_name_with_slashes = (
            path.parent if path.name in ("__init__.py", "__init__.pyi") else path.with_suffix("")
        )
        return cls(module_name_with_slashes.as_posix().replace("/", "."))


@dataclass(frozen=True)
class AllPythonTargets:
    first_party: tuple[Target, ...]
    third_party: tuple[Target, ...]


@rule(desc="Find all Python targets in project", level=LogLevel.DEBUG)
def find_all_python_projects(all_targets: AllTargets) -> AllPythonTargets:
    first_party = []
    third_party = []
    for tgt in all_targets:
        if tgt.has_field(PythonSourceField):
            first_party.append(tgt)
        if tgt.has_field(PythonRequirementsField):
            third_party.append(tgt)
    return AllPythonTargets(tuple(first_party), tuple(third_party))


# -----------------------------------------------------------------------------------------------
# First-party module mapping
# -----------------------------------------------------------------------------------------------


class FirstPartyPythonMappingImpl(FrozenDict[str, Tuple[ModuleProvider, ...]]):
    """A mapping of module names to owning addresses that a specific implementation adds for Python
    import dependency inference."""


@union
class FirstPartyPythonMappingImplMarker:
    """An entry point for a specific implementation of mapping module names to owning targets for
    Python import dependency inference.

    All implementations will be merged together. Any modules that show up in multiple
    implementations will be marked ambiguous.
    """


class FirstPartyPythonModuleMapping(FrozenDict[str, Tuple[ModuleProvider, ...]]):
    """A merged mapping of module names to owning addresses.

    This mapping may have been constructed from multiple distinct implementations, e.g.
    implementations for each codegen backends.
    """

    def providers_for_module(self, module: str) -> tuple[ModuleProvider, ...]:
        result = self.get(module, ())
        if result:
            return result

        # If the module is not found, try the parent, if any. This is to accommodate `from`
        # imports, where we don't care about the specific symbol, but only the module. For example,
        # with `from my_project.app import App`, we only care about the `my_project.app` part.
        #
        # We do not look past the direct parent, as this could cause multiple ambiguous owners to
        # be resolved. This contrasts with the third-party module mapping, which will try every
        # ancestor.
        if "." not in module:
            return ()
        parent_module = module.rsplit(".", maxsplit=1)[0]
        return self.get(parent_module, ())


@rule(level=LogLevel.DEBUG)
async def merge_first_party_module_mappings(
    union_membership: UnionMembership,
) -> FirstPartyPythonModuleMapping:
    all_mappings = await MultiGet(
        Get(
            FirstPartyPythonMappingImpl,
            FirstPartyPythonMappingImplMarker,
            marker_cls(),
        )
        for marker_cls in union_membership.get(FirstPartyPythonMappingImplMarker)
    )
    modules_to_providers: DefaultDict[str, list[ModuleProvider]] = defaultdict(list)
    for mapping_impl in all_mappings:
        for module, providers in mapping_impl.items():
            modules_to_providers[module].extend(providers)
    return FirstPartyPythonModuleMapping(
        (k, tuple(sorted(v))) for k, v in sorted(modules_to_providers.items())
    )


# This is only used to register our implementation with the plugin hook via unions. Note that we
# implement this like any other plugin implementation so that we can run them all in parallel.
class FirstPartyPythonTargetsMappingMarker(FirstPartyPythonMappingImplMarker):
    pass


@rule(desc="Creating map of first party Python targets to Python modules", level=LogLevel.DEBUG)
async def map_first_party_python_targets_to_modules(
    _: FirstPartyPythonTargetsMappingMarker, all_python_targets: AllPythonTargets
) -> FirstPartyPythonMappingImpl:
    stripped_file_per_target = await MultiGet(
        Get(StrippedFileName, StrippedFileNameRequest(tgt[PythonSourceField].file_path))
        for tgt in all_python_targets.first_party
    )

    modules_to_providers: DefaultDict[str, list[ModuleProvider]] = defaultdict(list)
    for tgt, stripped_file in zip(all_python_targets.first_party, stripped_file_per_target):
        stripped_f = PurePath(stripped_file.value)
        provider_type = (
            ModuleProviderType.TYPE_STUB if stripped_f.suffix == ".pyi" else ModuleProviderType.IMPL
        )
        module = PythonModule.create_from_stripped_path(stripped_f).module
        modules_to_providers[module].append(ModuleProvider(tgt.address, provider_type))

    return FirstPartyPythonMappingImpl(
        (k, tuple(sorted(v))) for k, v in sorted(modules_to_providers.items())
    )


# -----------------------------------------------------------------------------------------------
# Third party module mapping
# -----------------------------------------------------------------------------------------------

_ResolveName = str


class ThirdPartyPythonModuleMapping(
    FrozenDict[_ResolveName, FrozenDict[str, Tuple[ModuleProvider, ...]]]
):
    """A mapping of each resolve to the modules they contain and the addresses providing those
    modules."""

    def _providers_for_resolve(self, module: str, resolve: str) -> tuple[ModuleProvider, ...]:
        mapping = self.get(resolve)
        if not mapping:
            return ()

        result = mapping.get(module, ())
        if result:
            return result

        # If the module is not found, recursively try the ancestor modules, if any. For example,
        # pants.task.task.Task -> pants.task.task -> pants.task -> pants
        if "." not in module:
            return ()
        parent_module = module.rsplit(".", maxsplit=1)[0]
        return self._providers_for_resolve(parent_module, resolve)

    def providers_for_module(
        self, module: str, resolves: Iterable[str] | None
    ) -> tuple[ModuleProvider, ...]:
        """Find all providers for the module.

        If `resolves` is None, will not consider resolves, i.e. any `python_requirement` can be
        consumed. Otherwise, providers can only come from `python_requirements` marked compatible
        with those resolves.
        """
        if resolves is None:
            resolves = list(self.keys())
        return tuple(
            itertools.chain.from_iterable(
                self._providers_for_resolve(module, resolve) for resolve in resolves
            )
        )


@rule(desc="Creating map of third party targets to Python modules", level=LogLevel.DEBUG)
async def map_third_party_modules_to_addresses(
    all_python_tgts: AllPythonTargets,
    python_setup: PythonSetup,
) -> ThirdPartyPythonModuleMapping:
    resolves_to_modules_to_providers: dict[
        _ResolveName, DefaultDict[str, list[ModuleProvider]]
    ] = {}

    for tgt in all_python_tgts.third_party:
        tgt[PythonRequirementCompatibleResolvesField].validate(python_setup)
        resolves = tgt[PythonRequirementCompatibleResolvesField].value_or_default(python_setup)

        def add_modules(modules: Iterable[str], *, type_stub: bool = False) -> None:
            for resolve in resolves:
                if resolve not in resolves_to_modules_to_providers:
                    resolves_to_modules_to_providers[resolve] = defaultdict(list)
                for module in modules:
                    resolves_to_modules_to_providers[resolve][module].append(
                        ModuleProvider(
                            tgt.address,
                            ModuleProviderType.TYPE_STUB if type_stub else ModuleProviderType.IMPL,
                        )
                    )

        explicit_modules = tgt.get(PythonRequirementModulesField).value
        if explicit_modules:
            add_modules(explicit_modules)
            continue

        explicit_stub_modules = tgt.get(PythonRequirementTypeStubModulesField).value
        if explicit_stub_modules:
            add_modules(explicit_stub_modules, type_stub=True)
            continue

        # Else, fall back to defaults.
        for req in tgt[PythonRequirementsField].value:
            # NB: We don't use `canonicalize_project_name()` for the fallback value because we
            # want to preserve `.` in the module name. See
            # https://www.python.org/dev/peps/pep-0503/#normalized-names.
            proj_name = canonicalize_project_name(req.project_name)
            fallback_value = req.project_name.strip().lower().replace("-", "_")

            in_stubs_map = proj_name in DEFAULT_TYPE_STUB_MODULE_MAPPING
            starts_with_prefix = fallback_value.startswith(("types_", "stubs_"))
            ends_with_prefix = fallback_value.endswith(("_types", "_stubs"))
            if proj_name not in DEFAULT_MODULE_MAPPING and (
                in_stubs_map or starts_with_prefix or ends_with_prefix
            ):
                if in_stubs_map:
                    stub_modules = DEFAULT_TYPE_STUB_MODULE_MAPPING[proj_name]
                else:
                    stub_modules = (
                        fallback_value[6:] if starts_with_prefix else fallback_value[:-6],
                    )
                add_modules(stub_modules, type_stub=True)
            else:
                add_modules(DEFAULT_MODULE_MAPPING.get(proj_name, (fallback_value,)))

    return ThirdPartyPythonModuleMapping(
        (
            resolve,
            FrozenDict(
                (mod, tuple(sorted(providers))) for mod, providers in sorted(mapping.items())
            ),
        )
        for resolve, mapping in sorted(resolves_to_modules_to_providers.items())
    )


# -----------------------------------------------------------------------------------------------
# module -> owners
# -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonModuleOwners:
    """The target(s) that own a Python module.

    If >1 targets own the same module, and they're implementations (vs .pyi type stubs), they will
    be put into `ambiguous` instead of `unambiguous`. `unambiguous` should never be > 2.
    """

    unambiguous: tuple[Address, ...]
    ambiguous: tuple[Address, ...] = ()

    def __post_init__(self) -> None:
        if self.unambiguous and self.ambiguous:
            raise AssertionError(
                "A module has both unambiguous and ambiguous owners, which is a bug in the "
                "dependency inference code. Please file a bug report at "
                "https://github.com/pantsbuild/pants/issues/new."
            )


@rule
async def map_module_to_address(
    module: PythonModule,
    first_party_mapping: FirstPartyPythonModuleMapping,
    third_party_mapping: ThirdPartyPythonModuleMapping,
) -> PythonModuleOwners:
    providers = [
        *third_party_mapping.providers_for_module(module.module, resolves=None),
        *first_party_mapping.providers_for_module(module.module),
    ]
    addresses = tuple(provider.addr for provider in providers)

    # There's no ambiguity if there are only 0-1 providers.
    if len(providers) < 2:
        return PythonModuleOwners(addresses)

    # Else, it's ambiguous unless there are exactly two providers and one is a type stub and the
    # other an implementation.
    if len(providers) == 2 and (providers[0].typ == ModuleProviderType.TYPE_STUB) ^ (
        providers[1].typ == ModuleProviderType.TYPE_STUB
    ):
        return PythonModuleOwners(addresses)

    return PythonModuleOwners((), ambiguous=addresses)


def rules():
    return (
        *collect_rules(),
        UnionRule(FirstPartyPythonMappingImplMarker, FirstPartyPythonTargetsMappingMarker),
    )
