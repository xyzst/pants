# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import json
import logging
import os.path
from dataclasses import dataclass

from pants.backend.java.dependency_inference import java_parser_launcher
from pants.backend.java.dependency_inference.java_parser_launcher import (
    JavaParserCompiledClassfiles,
    java_parser_artifact_requirements,
)
from pants.backend.java.dependency_inference.types import JavaSourceDependencyAnalysis
from pants.core.util_rules.source_files import SourceFiles
from pants.engine.fs import AddPrefix, Digest, DigestContents
from pants.engine.process import BashBinary, FallibleProcessResult, Process, ProcessExecutionFailure
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.jvm.jdk_rules import JdkSetup
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.option.global_options import ProcessCleanupOption
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JavaSourceDependencyAnalysisRequest:
    source_files: SourceFiles


@dataclass(frozen=True)
class FallibleJavaSourceDependencyAnalysisResult:
    process_result: FallibleProcessResult


@rule(level=LogLevel.DEBUG)
async def resolve_fallible_result_to_analysis(
    fallible_result: FallibleJavaSourceDependencyAnalysisResult,
    process_cleanup: ProcessCleanupOption,
) -> JavaSourceDependencyAnalysis:
    # TODO(#12725): Just convert directly to a ProcessResult like this:
    # result = await Get(ProcessResult, FallibleProcessResult, fallible_result.process_result)
    if fallible_result.process_result.exit_code == 0:
        analysis_contents = await Get(
            DigestContents, Digest, fallible_result.process_result.output_digest
        )
        analysis = json.loads(analysis_contents[0].content)
        return JavaSourceDependencyAnalysis.from_json_dict(analysis)
    raise ProcessExecutionFailure(
        fallible_result.process_result.exit_code,
        fallible_result.process_result.stdout,
        fallible_result.process_result.stderr,
        "Java source dependency analysis failed.",
        process_cleanup=process_cleanup.val,
    )


@rule(level=LogLevel.DEBUG)
async def make_analysis_request_from_source_files(
    source_files: SourceFiles,
) -> JavaSourceDependencyAnalysisRequest:
    return JavaSourceDependencyAnalysisRequest(source_files=source_files)


@rule(level=LogLevel.DEBUG)
async def analyze_java_source_dependencies(
    bash: BashBinary,
    jdk_setup: JdkSetup,
    processor_classfiles: JavaParserCompiledClassfiles,
    request: JavaSourceDependencyAnalysisRequest,
) -> FallibleJavaSourceDependencyAnalysisResult:
    source_files = request.source_files
    if len(source_files.files) > 1:
        raise ValueError(
            f"parse_java_package expects sources with exactly 1 source file, but found {len(source_files.files)}."
        )
    elif len(source_files.files) == 0:
        raise ValueError(
            "parse_java_package expects sources with exactly 1 source file, but found none."
        )
    source_prefix = "__source_to_analyze"
    source_path = os.path.join(source_prefix, source_files.files[0])
    processorcp_relpath = "__processorcp"
    toolcp_relpath = "__toolcp"

    (tool_classpath, prefixed_source_files_digest,) = await MultiGet(
        Get(
            ToolClasspath,
            ToolClasspathRequest(artifact_requirements=java_parser_artifact_requirements()),
        ),
        Get(Digest, AddPrefix(source_files.snapshot.digest, source_prefix)),
    )

    immutable_input_digests = {
        **jdk_setup.immutable_input_digests,
        toolcp_relpath: tool_classpath.digest,
        processorcp_relpath: processor_classfiles.digest,
    }

    analysis_output_path = "__source_analysis.json"

    process_result = await Get(
        FallibleProcessResult,
        Process(
            argv=[
                *jdk_setup.args(
                    bash, [*tool_classpath.classpath_entries(toolcp_relpath), processorcp_relpath]
                ),
                "org.pantsbuild.javaparser.PantsJavaParserLauncher",
                analysis_output_path,
                source_path,
            ],
            input_digest=prefixed_source_files_digest,
            immutable_input_digests=immutable_input_digests,
            output_files=(analysis_output_path,),
            use_nailgun=immutable_input_digests.keys(),
            append_only_caches=jdk_setup.append_only_caches,
            env=jdk_setup.env,
            description=f"Analyzing {source_files.files[0]}",
            level=LogLevel.DEBUG,
        ),
    )

    return FallibleJavaSourceDependencyAnalysisResult(process_result=process_result)


def rules():
    return [
        *collect_rules(),
        *java_parser_launcher.rules(),
    ]
