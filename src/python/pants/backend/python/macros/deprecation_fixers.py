# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os.path
import tokenize
from dataclasses import dataclass

from pants.backend.python.target_types import PythonRequirementsFileTarget, PythonRequirementTarget
from pants.build_graph.address import InvalidAddress
from pants.core.goals.update_build_files import (
    DeprecationFixerRequest,
    RewrittenBuildFile,
    RewrittenBuildFileRequest,
    UpdateBuildFilesSubsystem,
)
from pants.engine.addresses import Address, Addresses, AddressInput, BuildFileAddress
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    AllTargets,
    Dependencies,
    DependenciesRequest,
    ExplicitlyProvidedDependencies,
    SingleSourceField,
    UnexpandedTargets,
)
from pants.engine.unions import UnionRule
from pants.option.global_options import GlobalOptions
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.strutil import bullet_list

logger = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class GeneratorRename:
    build_path: str
    alias: str
    new_name: str | None


@dataclass(frozen=True)
class MacroRenames:
    generators: tuple[GeneratorRename, ...]
    # Includes the alias for the macro.
    generated: FrozenDict[Address, tuple[Address, str]]


class MacroRenamesRequest:
    pass


@rule(desc="Determine how to rename Python macros to target generators", level=LogLevel.DEBUG)
async def determine_macro_changes(all_targets: AllTargets, _: MacroRenamesRequest) -> MacroRenames:
    # Strategy: Find `python_requirement` targets who depend on a `_python_requirements_file`
    # target to figure out which macros we have. Note that context-aware object factories (CAOFs)
    # are not actual targets and are "erased", so this is the way to find the macros.
    #
    # We also need to figure out if the new target generator can use the default `name=None` or
    # if it needs to set an explicit name, based on whether it's the build root and whether the
    # default is already taken.

    dirs_with_default_name = set()
    python_requirement_dependencies_fields = set()
    for tgt in all_targets:
        if tgt.address.is_default_target:
            dirs_with_default_name.add(tgt.address.spec_path)
        if isinstance(tgt, PythonRequirementTarget) and tgt[Dependencies].value is not None:
            python_requirement_dependencies_fields.add(tgt[Dependencies])

    build_file_addresses_per_tgt = await MultiGet(
        Get(BuildFileAddress, Address, deps_field.address)
        for deps_field in python_requirement_dependencies_fields
    )
    explicit_deps_per_tgt = await MultiGet(
        Get(ExplicitlyProvidedDependencies, DependenciesRequest(deps_field))
        for deps_field in python_requirement_dependencies_fields
    )
    deps_per_tgt = await MultiGet(
        Get(UnexpandedTargets, Addresses(explicit_deps.includes))
        for explicit_deps in explicit_deps_per_tgt
    )

    generators = set()
    generated = {}
    for python_req_deps_field, build_file_addr, deps in zip(
        python_requirement_dependencies_fields, build_file_addresses_per_tgt, deps_per_tgt
    ):
        generator_tgt = next(
            (tgt for tgt in deps if isinstance(tgt, PythonRequirementsFileTarget)), None
        )
        if generator_tgt is None:
            continue

        generator_source = generator_tgt[SingleSourceField].value
        if "Pipfile" in generator_source:
            generator_alias = "pipenv_requirements"
        elif "pyproject.toml" in generator_source:
            generator_alias = "poetry_requirements"
        # It's common to override `source=` for `python_requirements` to something other than
        # `requirements.txt`. Hence why we don't use `elif` to check for a certain file name.
        else:
            generator_alias = "python_requirements"

        # TODO: Robustly handle if the `name` is already claimed? This can happen, for example,
        #  if you have two `python_requirements` in the same BUILD file. Perhaps error?
        generator_name: str | None
        if (
            generator_tgt.address.spec_path
            and generator_tgt.address.spec_path not in dirs_with_default_name
        ):
            generator_name = None
        elif generator_alias == "pipenv_requirements":
            generator_name = "pipenv"
        elif generator_alias == "poetry_requirements":
            generator_name = "poetry"
        else:
            generator_name = "reqs"

        generators.add(GeneratorRename(build_file_addr.rel_path, generator_alias, generator_name))

        new_addr = Address(
            generator_tgt.address.spec_path,
            target_name=generator_name,
            generated_name=python_req_deps_field.address.target_name,
        )
        generated[python_req_deps_field.address] = (new_addr, generator_alias)

    generators_that_need_renames = sorted(
        generator for generator in generators if generator.new_name is not None
    )
    if generators_that_need_renames:
        changes = bullet_list(
            f'`{generator.alias}` in {generator.build_path}: add `name="{generator.new_name}"'
            for generator in generators_that_need_renames
        )
        logger.error(
            "You must manually add the `name=` field to the following targets. This is not done "
            f"automatically by the `update-build-files` goal.\n\n{changes}"
        )

    return MacroRenames(tuple(sorted(generators)), FrozenDict(sorted(generated.items())))


class UpdatePythonMacrosRequest(DeprecationFixerRequest):
    pass


@rule(desc="Change Python macros to target generators", level=LogLevel.DEBUG)
async def maybe_update_macros_references(
    request: UpdatePythonMacrosRequest,
    global_options: GlobalOptions,
    update_build_files_subsystem: UpdateBuildFilesSubsystem,
) -> RewrittenBuildFile:
    if not update_build_files_subsystem.fix_python_macros:
        return RewrittenBuildFile(request.path, request.lines, ())

    if not global_options.options.use_deprecated_python_macros:
        raise ValueError(
            "`--update-build-files-fix-python-macros` specified when "
            "`[GLOBAL].use_deprecated_python_macros` is already set to false, which means that "
            "there is nothing left to fix."
        )

    renames = await Get(MacroRenames, MacroRenamesRequest())

    changed_generator_aliases = set()

    def maybe_update(input_lines: tuple[str, ...]) -> list[str]:
        tokens = UpdatePythonMacrosRequest("", input_lines, colors_enabled=False).tokenize()
        updated_text_lines = list(input_lines)
        changed_line_indexes = set()
        for token in tokens:
            if token.type is not tokenize.STRING:
                continue
            line_index = token.start[0] - 1
            line = input_lines[line_index]

            # The `prefix` and `suffix` include the quotes for the string.
            prefix = line[: token.start[1] + 1]
            val = line[token.start[1] + 1 : token.end[1] - 1]
            suffix = line[token.end[1] - 1 :]

            # All macros generate targets with a `name`, so we know they must have `:`. We know they
            # also can't have `#` because they're not generated targets syntax.
            if ":" not in val or "#" in val:
                continue

            try:
                # We assume that all addresses are normal addresses, rather than file addresses, as
                # we know that none of the generated targets will be file addresses. That is, we can
                # ignore file addresses.
                addr = AddressInput.parse(
                    val, relative_to=os.path.dirname(request.path)
                ).dir_to_address()
            except InvalidAddress:
                continue

            if addr not in renames.generated:
                continue

            # If this line has already been changed, we need to re-tokenize it before we can
            # apply the change. Otherwise, we'll overwrite the prior change.
            if line_index in changed_line_indexes:
                return maybe_update(tuple(updated_text_lines))

            new_addr, generator_alias = renames.generated[addr]

            # Preserve relative addresses (`:tgt`), else use the normalized spec.
            if val.startswith(":"):
                new_val = (
                    f"#{new_addr.generated_name}"
                    if new_addr.is_default_target
                    else f":{new_addr.target_name}#{new_addr.generated_name}"
                )
            else:
                new_val = new_addr.spec

            updated_text_lines[line_index] = f"{prefix}{new_val}{suffix}"
            changed_line_indexes.add(line_index)
            changed_generator_aliases.add(generator_alias)

        return updated_text_lines

    return RewrittenBuildFile(
        request.path,
        tuple(maybe_update(request.lines)),
        change_descriptions=tuple(
            f"Update references to targets generated by `{request.red(alias)}`"
            for alias in changed_generator_aliases
        ),
    )


def rules():
    return (
        *collect_rules(),
        UnionRule(RewrittenBuildFileRequest, UpdatePythonMacrosRequest),
    )
