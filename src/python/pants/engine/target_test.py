# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from collections import namedtuple
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pytest

from pants.engine.addresses import Address
from pants.engine.fs import GlobExpansionConjunction, GlobMatchErrorBehavior, Paths
from pants.engine.target import (
    AsyncFieldMixin,
    BoolField,
    Dependencies,
    DictStringToStringField,
    DictStringToStringSequenceField,
    ExplicitlyProvidedDependencies,
    Field,
    FieldSet,
    FloatField,
    GeneratedTargets,
    GenerateSourcesRequest,
    IntField,
    InvalidFieldChoiceException,
    InvalidFieldException,
    InvalidFieldTypeException,
    InvalidGeneratedTargetException,
    InvalidTargetException,
    MultipleSourcesField,
    NestedDictStringToStringField,
    OptionalSingleSourceField,
    OverridesField,
    RequiredFieldMissingException,
    ScalarField,
    SequenceField,
    SingleSourceField,
    StringField,
    StringSequenceField,
    Tags,
    Target,
    ValidNumbers,
    generate_file_level_targets,
    targets_with_sources_types,
)
from pants.engine.unions import UnionMembership
from pants.option.global_options import FilesNotFoundBehavior
from pants.testutil.pytest_util import no_exception
from pants.util.frozendict import FrozenDict
from pants.util.meta import FrozenInstanceError
from pants.util.ordered_set import FrozenOrderedSet, OrderedSet

# -----------------------------------------------------------------------------------------------
# Test core Field and Target abstractions
# -----------------------------------------------------------------------------------------------


class FortranExtensions(Field):
    alias = "fortran_extensions"
    value: Tuple[str, ...]
    default = ()

    @classmethod
    def compute_value(cls, raw_value: Optional[Iterable[str]], address: Address) -> Tuple[str, ...]:
        value_or_default = super().compute_value(raw_value, address)
        # Add some arbitrary validation to test that hydration/validation works properly.
        bad_extensions = [
            extension for extension in value_or_default if not extension.startswith("Fortran")
        ]
        if bad_extensions:
            raise InvalidFieldException(
                f"The {repr(cls.alias)} field in target {address} expects all elements to be "
                f"prefixed by `Fortran`. Received {bad_extensions}.",
            )
        return tuple(value_or_default)


class FortranVersion(StringField):
    alias = "version"


class UnrelatedField(BoolField):
    alias = "unrelated"
    default = False


class FortranTarget(Target):
    alias = "fortran"
    core_fields = (FortranExtensions, FortranVersion)

    def validate(self) -> None:
        if self[FortranVersion].value == "bad":
            raise InvalidTargetException("Bad!")


def test_field_and_target_eq() -> None:
    addr = Address("", target_name="tgt")
    field = FortranVersion("dev0", addr)
    assert field.value == "dev0"

    other = FortranVersion("dev0", addr)
    assert field == other
    assert hash(field) == hash(other)

    other = FortranVersion("dev1", addr)
    assert field != other
    assert hash(field) != hash(other)

    # NB: because normal `Field`s throw away the address, these are equivalent.
    other = FortranVersion("dev0", Address("", target_name="other"))
    assert field == other
    assert hash(field) == hash(other)

    # Ensure the field is frozen.
    with pytest.raises(FrozenInstanceError):
        field.y = "foo"  # type: ignore[attr-defined]

    tgt = FortranTarget({"version": "dev0"}, addr)
    assert tgt.address == addr

    other_tgt = FortranTarget({"version": "dev0"}, addr)
    assert tgt == other_tgt
    assert hash(tgt) == hash(other_tgt)

    other_tgt = FortranTarget({"version": "dev1"}, addr)
    assert tgt != other_tgt
    assert hash(tgt) != hash(other_tgt)

    other_tgt = FortranTarget({"version": "dev0"}, Address("", target_name="other"))
    assert tgt != other_tgt
    assert hash(tgt) != hash(other_tgt)

    # Ensure the target is frozen.
    with pytest.raises(FrozenInstanceError):
        tgt.y = "foo"  # type: ignore[attr-defined]

    # Ensure that subclasses are not equal.
    class SubclassField(FortranVersion):
        pass

    subclass_field = SubclassField("dev0", addr)
    assert field != subclass_field
    assert hash(field) != hash(subclass_field)

    class SubclassTarget(FortranTarget):
        pass

    subclass_tgt = SubclassTarget({"version": "dev0"}, addr)
    assert tgt != subclass_tgt
    assert hash(tgt) != hash(subclass_tgt)


def test_invalid_fields_rejected() -> None:
    with pytest.raises(InvalidFieldException) as exc:
        FortranTarget({"invalid_field": True}, Address("", target_name="lib"))
    assert "Unrecognized field `invalid_field=True`" in str(exc)
    assert "//:lib" in str(exc)


def test_get_field() -> None:
    extensions = ("FortranExt1",)
    tgt = FortranTarget({FortranExtensions.alias: extensions}, Address("", target_name="lib"))

    assert tgt[FortranExtensions].value == extensions
    assert tgt.get(FortranExtensions).value == extensions
    assert tgt.get(FortranExtensions, default_raw_value=["FortranExt2"]).value == extensions

    # Default field value. This happens when the field is registered on the target type, but the
    # user does not explicitly set the field in the BUILD file.
    default_field_tgt = FortranTarget({}, Address("", target_name="default"))
    assert default_field_tgt[FortranExtensions].value == ()
    assert default_field_tgt.get(FortranExtensions).value == ()
    assert default_field_tgt.get(FortranExtensions, default_raw_value=["FortranExt2"]).value == ()
    # Example of a call site applying its own default value instead of the field's default value.
    assert default_field_tgt[FortranExtensions].value or 123 == 123

    assert (
        FortranTarget.class_get_field(FortranExtensions, union_membership=UnionMembership({}))
        is FortranExtensions
    )

    # Field is not registered on the target.
    with pytest.raises(KeyError) as exc:
        default_field_tgt[UnrelatedField]
    assert UnrelatedField.__name__ in str(exc)

    with pytest.raises(KeyError) as exc:
        FortranTarget.class_get_field(UnrelatedField, union_membership=UnionMembership({}))
    assert UnrelatedField.__name__ in str(exc)

    assert default_field_tgt.get(UnrelatedField).value == UnrelatedField.default
    assert default_field_tgt.get(
        UnrelatedField, default_raw_value=not UnrelatedField.default
    ).value == (not UnrelatedField.default)


def test_field_hydration_is_eager() -> None:
    with pytest.raises(InvalidFieldException) as exc:
        FortranTarget(
            {FortranExtensions.alias: ["FortranExt1", "DoesNotStartWithFortran"]},
            Address("", target_name="bad_extension"),
        )
    assert "DoesNotStartWithFortran" in str(exc)
    assert "//:bad_extension" in str(exc)


def test_has_fields() -> None:
    empty_union_membership = UnionMembership({})
    tgt = FortranTarget({}, Address("", target_name="lib"))

    assert tgt.field_types == (FortranExtensions, FortranVersion)
    assert FortranTarget.class_field_types(union_membership=empty_union_membership) == (
        FortranExtensions,
        FortranVersion,
    )

    assert tgt.has_fields([]) is True
    assert FortranTarget.class_has_fields([], union_membership=empty_union_membership) is True

    assert tgt.has_fields([FortranExtensions]) is True
    assert tgt.has_field(FortranExtensions) is True
    assert (
        FortranTarget.class_has_fields([FortranExtensions], union_membership=empty_union_membership)
        is True
    )
    assert (
        FortranTarget.class_has_field(FortranExtensions, union_membership=empty_union_membership)
        is True
    )

    assert tgt.has_fields([UnrelatedField]) is False
    assert tgt.has_field(UnrelatedField) is False
    assert (
        FortranTarget.class_has_fields([UnrelatedField], union_membership=empty_union_membership)
        is False
    )
    assert (
        FortranTarget.class_has_field(UnrelatedField, union_membership=empty_union_membership)
        is False
    )

    assert tgt.has_fields([FortranExtensions, UnrelatedField]) is False
    assert (
        FortranTarget.class_has_fields(
            [FortranExtensions, UnrelatedField], union_membership=empty_union_membership
        )
        is False
    )


def test_add_custom_fields() -> None:
    class CustomField(BoolField):
        alias = "custom_field"
        default = False

    union_membership = UnionMembership.from_rules(
        [FortranTarget.register_plugin_field(CustomField)]
    )
    tgt_values = {CustomField.alias: True}
    tgt = FortranTarget(
        tgt_values, Address("", target_name="lib"), union_membership=union_membership
    )

    assert tgt.field_types == (FortranExtensions, FortranVersion, CustomField)
    assert tgt.core_fields == (FortranExtensions, FortranVersion)
    assert tgt.plugin_fields == (CustomField,)
    assert tgt.has_field(CustomField) is True

    assert FortranTarget.class_field_types(union_membership=union_membership) == (
        FortranExtensions,
        FortranVersion,
        CustomField,
    )
    assert FortranTarget.class_has_field(CustomField, union_membership=union_membership) is True
    assert (
        FortranTarget.class_get_field(CustomField, union_membership=union_membership) is CustomField
    )

    assert tgt[CustomField].value is True

    default_tgt = FortranTarget(
        {}, Address("", target_name="default"), union_membership=union_membership
    )
    assert default_tgt[CustomField].value is False

    # Ensure that the `PluginField` is not being registered on other target types.
    class OtherTarget(Target):
        alias = "other_target"
        core_fields = ()

    other_tgt = OtherTarget({}, Address("", target_name="other"))
    assert other_tgt.plugin_fields == ()
    assert other_tgt.has_field(CustomField) is False


def test_override_preexisting_field_via_new_target() -> None:
    # To change the behavior of a pre-existing field, you must create a new target as it would not
    # be safe to allow plugin authors to change the behavior of core target types.
    #
    # Because the Target API does not care about the actual target type and we only check that the
    # target has the required fields via Target.has_fields(), it is safe to create a new target
    # that still works where the original target was expected.
    #
    # However, this means that we must ensure `Target.get()` and `Target.has_fields()` will work
    # with subclasses of the original `Field`s.

    class CustomFortranExtensions(FortranExtensions):
        banned_extensions = ("FortranBannedExt",)
        default_extensions = ("FortranCustomExt",)

        @classmethod
        def compute_value(
            cls, raw_value: Optional[Iterable[str]], address: Address
        ) -> Tuple[str, ...]:
            # Ensure that we avoid certain problematic extensions and always use some defaults.
            specified_extensions = super().compute_value(raw_value, address)
            banned = [
                extension
                for extension in specified_extensions
                if extension in cls.banned_extensions
            ]
            if banned:
                raise InvalidFieldException(
                    f"The {repr(cls.alias)} field in target {address} is using banned "
                    f"extensions: {banned}"
                )
            return (*specified_extensions, *cls.default_extensions)

    class CustomFortranTarget(Target):
        alias = "custom_fortran"
        core_fields = tuple(
            {*FortranTarget.core_fields, CustomFortranExtensions} - {FortranExtensions}
        )

    custom_tgt = CustomFortranTarget(
        {FortranExtensions.alias: ["FortranExt1"]}, Address("", target_name="custom")
    )

    assert custom_tgt.has_field(FortranExtensions) is True
    assert custom_tgt.has_field(CustomFortranExtensions) is True
    assert custom_tgt.has_fields([FortranExtensions, CustomFortranExtensions]) is True
    assert (
        CustomFortranTarget.class_get_field(FortranExtensions, union_membership=UnionMembership({}))
        is CustomFortranExtensions
    )

    # Ensure that subclasses not defined on a target are not accepted. This allows us to, for
    # example, filter every target with `PythonSources` (or a subclass) and to ignore targets with
    # only `SourcesField`.
    normal_tgt = FortranTarget({}, Address("", target_name="normal"))
    assert normal_tgt.has_field(FortranExtensions) is True
    assert normal_tgt.has_field(CustomFortranExtensions) is False

    assert custom_tgt[FortranExtensions] == custom_tgt[CustomFortranExtensions]
    assert custom_tgt[FortranExtensions].value == (
        "FortranExt1",
        *CustomFortranExtensions.default_extensions,
    )

    # Check custom default value
    assert (
        CustomFortranTarget({}, Address("", target_name="default"))[FortranExtensions].value
        == CustomFortranExtensions.default_extensions
    )

    # Custom validation
    with pytest.raises(InvalidFieldException) as exc:
        CustomFortranTarget(
            {FortranExtensions.alias: CustomFortranExtensions.banned_extensions},
            Address("", target_name="invalid"),
        )
    assert str(list(CustomFortranExtensions.banned_extensions)) in str(exc)
    assert "//:invalid" in str(exc)


def test_required_field() -> None:
    class RequiredField(StringField):
        alias = "field"
        required = True

    class RequiredTarget(Target):
        alias = "required_target"
        core_fields = (RequiredField,)

    address = Address("", target_name="lib")

    # No errors when defined
    RequiredTarget({"field": "present"}, address)

    with pytest.raises(RequiredFieldMissingException) as exc:
        RequiredTarget({}, address)
    assert str(address) in str(exc.value)
    assert "field" in str(exc.value)


def test_async_field_mixin() -> None:
    class ExampleField(IntField, AsyncFieldMixin):
        alias = "field"
        default = 10

    addr = Address("", target_name="tgt")
    field = ExampleField(None, addr)
    assert field.value == 10
    assert field.address == addr
    ExampleField.mro()  # Regression test that the mro is resolvable.

    # Ensure equality and __hash__ work correctly.
    other = ExampleField(None, addr)
    assert field == other
    assert hash(field) == hash(other)

    other = ExampleField(25, addr)
    assert field != other
    assert hash(field) != hash(other)

    # Whereas normally the address is not considered, it is considered for async fields.
    other = ExampleField(None, Address("", target_name="other"))
    assert field != other
    assert hash(field) != hash(other)

    # Ensure it's still frozen.
    with pytest.raises(FrozenInstanceError):
        field.y = "foo"  # type: ignore[attr-defined]

    # Ensure that subclasses are not equal.
    class Subclass(ExampleField):
        pass

    subclass = Subclass(None, addr)
    assert field != subclass
    assert hash(field) != hash(subclass)


def test_target_validate() -> None:
    with pytest.raises(InvalidTargetException):
        FortranTarget({FortranVersion.alias: "bad"}, Address("", target_name="t"))


def test_target_residence_dir() -> None:
    assert FortranTarget({}, Address("some_dir/subdir")).residence_dir == "some_dir/subdir"
    assert (
        FortranTarget({}, Address("some_dir/subdir"), residence_dir="another_dir").residence_dir
        == "another_dir"
    )


# -----------------------------------------------------------------------------------------------
# Test file-level target generation
# -----------------------------------------------------------------------------------------------


def test_generate_file_level_targets() -> None:
    class MockGenerator(Target):
        alias = "generator"
        core_fields = (Dependencies, Tags, MultipleSourcesField)

    class MockGenerated(Target):
        alias = "generated"
        core_fields = (Dependencies, Tags, MultipleSourcesField)

    def generate(
        generator: Target,
        files: List[str],
        *,
        add_dependencies_on_all_siblings: bool = False,
        use_generated_addr_syntax: bool = False,
        overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> GeneratedTargets:
        return generate_file_level_targets(
            MockGenerated,
            generator,
            files,
            None,
            add_dependencies_on_all_siblings=add_dependencies_on_all_siblings,
            use_generated_address_syntax=use_generated_addr_syntax,
            use_source_field=False,
            overrides=overrides,
        )

    tgt = MockGenerator(
        {MultipleSourcesField.alias: ["f1.ext", "f2.ext"], Tags.alias: ["tag"]}, Address("demo")
    )
    assert generate(tgt, ["demo/f1.ext", "demo/f2.ext"]) == GeneratedTargets(
        tgt,
        [
            MockGenerated(
                {MultipleSourcesField.alias: ["f1.ext"], Tags.alias: ["tag"]},
                Address("demo", relative_file_path="f1.ext"),
                residence_dir="demo",
            ),
            MockGenerated(
                {MultipleSourcesField.alias: ["f2.ext"], Tags.alias: ["tag"]},
                Address("demo", relative_file_path="f2.ext"),
                residence_dir="demo",
            ),
        ],
    )
    assert generate(
        tgt, ["demo/f1.ext", "demo/f2.ext"], add_dependencies_on_all_siblings=True
    ) == GeneratedTargets(
        tgt,
        [
            MockGenerated(
                {
                    MultipleSourcesField.alias: ["f1.ext"],
                    Dependencies.alias: ["demo/f2.ext"],
                    Tags.alias: ["tag"],
                },
                Address("demo", relative_file_path="f1.ext"),
                residence_dir="demo",
            ),
            MockGenerated(
                {
                    MultipleSourcesField.alias: ["f2.ext"],
                    Dependencies.alias: ["demo/f1.ext"],
                    Tags.alias: ["tag"],
                },
                Address("demo", relative_file_path="f2.ext"),
                residence_dir="demo",
            ),
        ],
    )

    subdir_tgt = MockGenerator(
        {MultipleSourcesField.alias: ["demo.f95", "subdir/demo.f95"]},
        Address("src/fortran", target_name="demo"),
    )
    assert generate(subdir_tgt, ["src/fortran/subdir/demo.f95"]) == GeneratedTargets(
        subdir_tgt,
        [
            MockGenerated(
                {MultipleSourcesField.alias: ["subdir/demo.f95"]},
                Address("src/fortran", target_name="demo", relative_file_path="subdir/demo.f95"),
                residence_dir="src/fortran/subdir",
            )
        ],
    )
    assert generate(
        subdir_tgt, ["src/fortran/subdir/demo.f95"], use_generated_addr_syntax=True
    ) == GeneratedTargets(
        subdir_tgt,
        [
            MockGenerated(
                {MultipleSourcesField.alias: ["subdir/demo.f95"]},
                Address("src/fortran", target_name="demo", generated_name="subdir/demo.f95"),
                residence_dir="src/fortran/subdir",
            )
        ],
    )

    # Can override fields.
    assert generate(
        tgt, ["demo/f1.ext"], overrides={"demo/f1.ext": {"tags": ["overridden"]}}
    ) == GeneratedTargets(
        tgt,
        [
            MockGenerated(
                {
                    MultipleSourcesField.alias: ["f1.ext"],
                    Tags.alias: ["overridden"],
                },
                Address("demo", relative_file_path="f1.ext"),
            ),
        ],
    )

    # The file path must match the filespec of the generator target's SourcesField.
    with pytest.raises(AssertionError) as exc:
        generate(tgt, ["demo/fake.ext"])
    assert "does not match a file demo/fake.ext" in str(exc.value)

    class MissingFieldsTarget(Target):
        alias = "missing_fields"
        core_fields = (Tags,)

    missing_fields_tgt = MissingFieldsTarget(
        {Tags.alias: ["tag"]}, Address("", target_name="missing_fields")
    )
    with pytest.raises(AssertionError) as exc:
        generate(missing_fields_tgt, ["fake.txt"])
    assert "does not have both a `dependencies` and `sources` field" in str(exc.value)


def test_generated_targets_address_validation() -> None:
    """Ensure that all addresses are well formed."""

    class MockTarget(Target):
        alias = "tgt"
        core_fields = ()

    generator = MockTarget({}, Address("dir", target_name="generator"))
    with pytest.raises(InvalidGeneratedTargetException):
        GeneratedTargets(
            generator,
            [
                MockTarget(
                    {}, Address("a_different_dir", target_name="generator", generated_name="gen")
                )
            ],
        )
    with pytest.raises(InvalidGeneratedTargetException):
        GeneratedTargets(
            generator,
            [
                MockTarget(
                    {}, Address("dir", target_name="a_different_generator", generated_name="gen")
                )
            ],
        )
    with pytest.raises(InvalidGeneratedTargetException):
        GeneratedTargets(
            generator,
            [
                MockTarget(
                    {},
                    Address(
                        "dir",
                        target_name="a_different_generator",
                        generated_name=None,
                        relative_file_path=None,
                    ),
                )
            ],
        )

    # These are fine.
    GeneratedTargets(
        generator,
        [
            MockTarget({}, Address("dir", target_name="generator", generated_name="gen")),
            MockTarget({}, Address("dir", target_name="generator", relative_file_path="gen")),
        ],
    )


def test_generated_targets_unused_overrides() -> None:
    class MockGenerator(Target):
        alias = "generator"
        core_fields = (Dependencies, Tags, MultipleSourcesField)

    class MockGenerated(Target):
        alias = "generated"
        core_fields = (Dependencies, Tags, SingleSourceField)

    with pytest.raises(InvalidFieldException) as exc:
        generate_file_level_targets(
            MockGenerated,
            MockGenerator(
                {MultipleSourcesField.alias: ["f*.ext"]}, Address("dir", target_name="gen")
            ),
            ["dir/f1.ext", "dir/f2.ext"],
            None,
            add_dependencies_on_all_siblings=False,
            overrides={"dir/fake.ext": {"tags": ["overridden"]}},
        )
    assert "Unused file paths in the `overrides` field for dir:gen: ['fake.ext']" in str(exc.value)


# -----------------------------------------------------------------------------------------------
# Test FieldSet. Also see engine/internals/graph_test.py.
# -----------------------------------------------------------------------------------------------


def test_field_set() -> None:
    class RequiredField(StringField):
        alias = "required_field"
        default = "default"

    class OptionalField(StringField):
        alias = "optional_field"
        default = "default"

    class OptOutField(BoolField):
        alias = "opt_out_field"
        default = False

    class TargetWithRequired(Target):
        alias = "tgt_w_required"
        # It has the required field registered, but not the optional field.
        core_fields = (RequiredField,)

    class TargetWithoutRequired(Target):
        alias = "tgt_wo_required"
        # It has the optional field registered, but not the required field.
        core_fields = (OptionalField,)

    class NoFieldsTarget(Target):
        alias = "no_fields_tgt"
        core_fields = ()

    class OptOutTarget(Target):
        alias = "opt_out_tgt"
        core_fields = (RequiredField, OptOutField)

    @dataclass(frozen=True)
    class RequiredFieldSet(FieldSet):
        required_fields = (RequiredField,)

        required: RequiredField
        optional: OptionalField

        @classmethod
        def opt_out(cls, tgt: Target) -> bool:
            return tgt.get(OptOutField).value is True

    @dataclass(frozen=True)
    class OptionalFieldSet(FieldSet):
        required_fields = ()

        optional: OptionalField

        @classmethod
        def opt_out(cls, tgt: Target) -> bool:
            return tgt.get(OptOutField).value is True

    required_addr = Address("", target_name="required")
    required_tgt = TargetWithRequired({RequiredField.alias: "configured"}, required_addr)
    optional_addr = Address("", target_name="unrelated")
    optional_tgt = TargetWithoutRequired({OptionalField.alias: "configured"}, optional_addr)
    no_fields_addr = Address("", target_name="no_fields")
    no_fields_tgt = NoFieldsTarget({}, no_fields_addr)
    opt_out_addr = Address("", target_name="conditional")
    opt_out_tgt = OptOutTarget(
        {RequiredField.alias: "configured", OptOutField.alias: True}, opt_out_addr
    )

    assert RequiredFieldSet.is_applicable(required_tgt) is True
    for tgt in [optional_tgt, no_fields_tgt, opt_out_tgt]:
        assert RequiredFieldSet.is_applicable(tgt) is False

    # When no fields are required, every target is applicable _unless_ it has been opted out of.
    for tgt in [required_tgt, optional_tgt, no_fields_tgt]:
        assert OptionalFieldSet.is_applicable(tgt) is True
    assert OptionalFieldSet.is_applicable(opt_out_tgt) is False

    required_fs = RequiredFieldSet.create(required_tgt)
    assert required_fs.address == required_addr
    assert required_fs.required.value == "configured"
    assert required_fs.optional.value == OptionalField.default
    assert isinstance(required_fs.required_fields, tuple)

    with pytest.raises(KeyError):
        RequiredFieldSet.create(optional_tgt)

    # It is possible to create a target that should be opted out of; the caller must call
    # `.is_applicable()` first.
    opt_out_fs = RequiredFieldSet.create(opt_out_tgt)
    assert opt_out_fs.address == opt_out_addr
    assert opt_out_fs.required.value == "configured"
    assert opt_out_fs.optional.value == OptionalField.default
    assert isinstance(required_fs.required_fields, tuple)

    assert OptionalFieldSet.create(optional_tgt).optional.value == "configured"
    assert OptionalFieldSet.create(no_fields_tgt).optional.value == OptionalField.default


# -----------------------------------------------------------------------------------------------
# Test Field templates
# -----------------------------------------------------------------------------------------------


def test_scalar_field() -> None:
    @dataclass(frozen=True)
    class CustomObject:
        pass

    class Example(ScalarField):
        alias = "example"
        expected_type = CustomObject
        expected_type_description = "a `CustomObject` instance"

        @classmethod
        def compute_value(
            cls, raw_value: Optional[CustomObject], address: Address
        ) -> Optional[CustomObject]:
            return super().compute_value(raw_value, address)

    addr = Address("", target_name="example")

    with pytest.raises(InvalidFieldTypeException) as exc:
        Example(1, addr)
    assert Example.expected_type_description in str(exc.value)

    assert Example(CustomObject(), addr).value == CustomObject()
    assert Example(None, addr).value is None


def test_string_field_valid_choices() -> None:
    class GivenStrings(StringField):
        alias = "example"
        valid_choices = ("kale", "spinach")

    class LeafyGreens(Enum):
        KALE = "kale"
        SPINACH = "spinach"

    class GivenEnum(StringField):
        alias = "example"
        valid_choices = LeafyGreens
        default = LeafyGreens.KALE.value

    addr = Address("", target_name="example")
    assert GivenStrings("spinach", addr).value == "spinach"
    assert GivenEnum("spinach", addr).value == "spinach"

    assert GivenStrings(None, addr).value is None
    assert GivenEnum(None, addr).value == "kale"

    with pytest.raises(InvalidFieldChoiceException):
        GivenStrings("carrot", addr)
    with pytest.raises(InvalidFieldChoiceException):
        GivenEnum("carrot", addr)


@pytest.mark.parametrize("field_cls", [IntField, FloatField])
def test_int_float_fields_valid_numbers(field_cls: type) -> None:
    class AllNums(field_cls):  # type: ignore[valid-type,misc]
        alias = "all_nums"
        valid_numbers = ValidNumbers.all

    class PositiveAndZero(field_cls):  # type: ignore[valid-type,misc]
        alias = "positive_and_zero"
        valid_numbers = ValidNumbers.positive_and_zero

    class PositiveOnly(field_cls):  # type: ignore[valid-type,misc]
        alias = "positive_only"
        valid_numbers = ValidNumbers.positive_only

    addr = Address("nums")
    neg = -1 if issubclass(field_cls, IntField) else -1.0
    zero = 0 if issubclass(field_cls, IntField) else 0.0
    pos = 1 if issubclass(field_cls, IntField) else 1.0

    assert AllNums(neg, addr).value == neg
    assert AllNums(zero, addr).value == zero
    assert AllNums(pos, addr).value == pos

    with pytest.raises(InvalidFieldException):
        PositiveAndZero(neg, addr)
    assert PositiveAndZero(zero, addr).value == zero
    assert PositiveAndZero(pos, addr).value == pos

    with pytest.raises(InvalidFieldException):
        PositiveOnly(neg, addr)
    with pytest.raises(InvalidFieldException):
        PositiveOnly(zero, addr)
    assert PositiveOnly(pos, addr).value == pos


def test_sequence_field() -> None:
    @dataclass(frozen=True)
    class CustomObject:
        pass

    class Example(SequenceField):
        alias = "example"
        expected_element_type = CustomObject
        expected_type_description = "an iterable of `CustomObject` instances"

        @classmethod
        def compute_value(
            cls, raw_value: Optional[Iterable[CustomObject]], address: Address
        ) -> Optional[Tuple[CustomObject, ...]]:
            return super().compute_value(raw_value, address)

    addr = Address("", target_name="example")

    def assert_flexible_constructor(raw_value: Iterable[CustomObject]) -> None:
        assert Example(raw_value, addr).value == tuple(raw_value)

    assert_flexible_constructor([CustomObject(), CustomObject()])
    assert_flexible_constructor((CustomObject(), CustomObject()))
    assert_flexible_constructor(OrderedSet([CustomObject(), CustomObject()]))

    # Must be given a sequence, not a single element.
    with pytest.raises(InvalidFieldTypeException) as exc:
        Example(CustomObject(), addr)
    assert Example.expected_type_description in str(exc.value)

    # All elements must be the expected type.
    with pytest.raises(InvalidFieldTypeException):
        Example([CustomObject(), 1, CustomObject()], addr)


def test_string_sequence_field() -> None:
    class Example(StringSequenceField):
        alias = "example"

    addr = Address("", target_name="example")
    assert Example(["hello", "world"], addr).value == ("hello", "world")
    assert Example(None, addr).value is None
    with pytest.raises(InvalidFieldTypeException):
        Example("strings are technically iterable...", addr)
    with pytest.raises(InvalidFieldTypeException):
        Example(["hello", 0, "world"], addr)


def test_dict_string_to_string_field() -> None:
    class Example(DictStringToStringField):
        alias = "example"

    addr = Address("", target_name="example")

    assert Example(None, addr).value is None
    assert Example({}, addr).value == FrozenDict()
    assert Example({"hello": "world"}, addr).value == FrozenDict({"hello": "world"})

    def assert_invalid_type(raw_value: Any) -> None:
        with pytest.raises(InvalidFieldTypeException):
            Example(raw_value, addr)

    for v in [0, object(), "hello", ["hello"], {"hello": 0}, {0: "world"}]:
        assert_invalid_type(v)

    # Regression test that a default can be set.
    class ExampleDefault(DictStringToStringField):
        alias = "example"
        # Note that we use `FrozenDict` so that the object can be hashable.
        default = FrozenDict({"default": "val"})

    assert ExampleDefault(None, addr).value == FrozenDict({"default": "val"})


def test_nested_dict_string_to_string_field() -> None:
    class Example(NestedDictStringToStringField):
        alias = "example"

    addr = Address("", target_name="example")

    assert Example(None, address=addr).value is None
    assert Example({}, address=addr).value == FrozenDict()
    assert Example({"greeting": {"hello": "world"}}, address=addr).value == FrozenDict(
        {"greeting": FrozenDict({"hello": "world"})}
    )

    def assert_invalid_type(raw_value: Any) -> None:
        with pytest.raises(InvalidFieldTypeException):
            Example(raw_value, address=addr)

    for v in [
        0,
        object(),
        "hello",
        ["hello"],
        ["hello", "world"],
        {"hello": 0},
        {0: "world"},
        {"hello": "world"},
    ]:
        assert_invalid_type(v)

    # Regression test that a default can be set.
    class ExampleDefault(NestedDictStringToStringField):
        alias = "example"
        # Note that we use `FrozenDict` so that the object can be hashable.
        default = FrozenDict({"nest": FrozenDict({"default": "val"})})

    assert ExampleDefault(None, address=addr).value == FrozenDict(
        {"nest": FrozenDict({"default": "val"})}
    )


def test_dict_string_to_string_sequence_field() -> None:
    class Example(DictStringToStringSequenceField):
        alias = "example"

    addr = Address("", target_name="example")

    def assert_flexible_constructor(raw_value: Dict[str, Iterable[str]]) -> None:
        assert Example(raw_value, addr).value == FrozenDict(
            {k: tuple(v) for k, v in raw_value.items()}
        )

    for v in [("hello", "world"), ["hello", "world"], OrderedSet(["hello", "world"])]:
        assert_flexible_constructor({"greeting": v})

    def assert_invalid_type(raw_value: Any) -> None:
        with pytest.raises(InvalidFieldTypeException):
            Example(raw_value, addr)

    for v in [  # type: ignore[assignment]
        0,
        object(),
        "hello",
        ["hello"],
        {"hello": "world"},
        {0: ["world"]},
    ]:
        assert_invalid_type(v)

    # Regression test that a default can be set.
    class ExampleDefault(DictStringToStringSequenceField):
        alias = "example"
        # Note that we use `FrozenDict` so that the object can be hashable.
        default = FrozenDict({"default": ("val",)})

    assert ExampleDefault(None, addr).value == FrozenDict({"default": ("val",)})


# -----------------------------------------------------------------------------------------------
# Test `SourcesField` helper functions
# -----------------------------------------------------------------------------------------------


def test_targets_with_sources_types() -> None:
    class Sources1(MultipleSourcesField):
        pass

    class Sources2(SingleSourceField):
        pass

    class CodegenSources(MultipleSourcesField):
        pass

    class Tgt1(Target):
        alias = "tgt1"
        core_fields = (Sources1,)

    class Tgt2(Target):
        alias = "tgt2"
        core_fields = (Sources2,)

    class CodegenTgt(Target):
        alias = "codegen_tgt"
        core_fields = (CodegenSources,)

    class GenSources(GenerateSourcesRequest):
        input = CodegenSources
        output = Sources1

    tgt1 = Tgt1({}, Address("tgt1"))
    tgt2 = Tgt2({SingleSourceField.alias: "foo.ext"}, Address("tgt2"))
    codegen_tgt = CodegenTgt({}, Address("codegen_tgt"))
    result = targets_with_sources_types(
        [Sources1],
        [tgt1, tgt2, codegen_tgt],
        union_membership=UnionMembership({GenerateSourcesRequest: [GenSources]}),
    )
    assert set(result) == {tgt1, codegen_tgt}

    result = targets_with_sources_types(
        [Sources2],
        [tgt1, tgt2, codegen_tgt],
        union_membership=UnionMembership({GenerateSourcesRequest: [GenSources]}),
    )
    assert set(result) == {tgt2}


SKIP = object()
expected_path_globs = namedtuple(
    "expected_path_globs",
    ["globs", "glob_match_error_behavior", "conjunction", "description_of_origin"],
    defaults=(SKIP, SKIP, SKIP, SKIP),
)


@pytest.mark.parametrize(
    "default_value, field_value, expected",
    [
        pytest.param(
            None,
            None,
            expected_path_globs(globs=()),
            id="empty",
        ),
        pytest.param(
            ["*"],
            None,
            expected_path_globs(
                globs=("test/*",),
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
                conjunction=GlobExpansionConjunction.any_match,
                description_of_origin=None,
            ),
            id="default ignores glob match error",
        ),
        pytest.param(
            ["*"],
            ["a", "b"],
            expected_path_globs(
                globs=(
                    "test/a",
                    "test/b",
                ),
                glob_match_error_behavior=GlobMatchErrorBehavior.warn,
                conjunction=GlobExpansionConjunction.all_match,
                description_of_origin="test:test's `sources` field",
            ),
            id="provided value warns on glob match error",
        ),
    ],
)
def test_multiple_sources_path_globs(
    default_value: Any, field_value: Any, expected: expected_path_globs
) -> None:
    class TestMultipleSourcesField(MultipleSourcesField):
        default = default_value
        default_glob_match_error_behavior = GlobMatchErrorBehavior.ignore

    sources = TestMultipleSourcesField(field_value, Address("test"))
    actual = sources.path_globs(FilesNotFoundBehavior.warn)
    for attr, expect in zip(expected._fields, expected):
        if expect is not SKIP:
            assert getattr(actual, attr) == expect


@pytest.mark.parametrize(
    "default_value, field_value, expected",
    [
        pytest.param(
            None,
            None,
            expected_path_globs(globs=()),
            id="empty",
        ),
        pytest.param(
            "file",
            None,
            expected_path_globs(
                globs=("test/file",),
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
                conjunction=GlobExpansionConjunction.any_match,
                description_of_origin=None,
            ),
            id="default ignores glob match error",
        ),
        pytest.param(
            "default_file",
            "other_file",
            expected_path_globs(
                globs=("test/other_file",),
                glob_match_error_behavior=GlobMatchErrorBehavior.warn,
                conjunction=GlobExpansionConjunction.all_match,
                description_of_origin="test:test's `source` field",
            ),
            id="provided value warns on glob match error",
        ),
        pytest.param(
            "file",
            "life",
            expected_path_globs(
                globs=("test/life",),
                glob_match_error_behavior=GlobMatchErrorBehavior.warn,
                conjunction=GlobExpansionConjunction.all_match,
                description_of_origin="test:test's `source` field",
            ),
            id="default glob conjunction",
        ),
    ],
)
def test_single_source_path_globs(
    default_value: Any, field_value: Any, expected: expected_path_globs
) -> None:
    class TestSingleSourceField(SingleSourceField):
        default = default_value
        default_glob_match_error_behavior = GlobMatchErrorBehavior.ignore
        required = False

    sources = TestSingleSourceField(field_value, Address("test"))

    actual = sources.path_globs(FilesNotFoundBehavior.warn)
    for attr, expect in zip(expected._fields, expected):
        if expect is not SKIP:
            assert getattr(actual, attr) == expect


def test_single_source_file_path() -> None:
    class TestSingleSourceField(OptionalSingleSourceField):
        pass

    assert TestSingleSourceField(None, Address("project")).file_path is None
    assert TestSingleSourceField("f.ext", Address("project")).file_path == "project/f.ext"


def test_single_source_field_bans_globs() -> None:
    class TestSingleSourceField(SingleSourceField):
        pass

    with pytest.raises(InvalidFieldException):
        TestSingleSourceField("*.ext", Address("project"))
    with pytest.raises(InvalidFieldException):
        TestSingleSourceField("!f.ext", Address("project"))


# -----------------------------------------------------------------------------------------------
# Test `ExplicitlyProvidedDependencies` helper functions
# -----------------------------------------------------------------------------------------------


def test_explicitly_provided_dependencies_any_are_covered_by_includes() -> None:
    addr = Address("", target_name="a")
    generated_addr = Address("", target_name="b", generated_name="gen")
    epd = ExplicitlyProvidedDependencies(
        Address("", target_name="input_tgt"),
        includes=FrozenOrderedSet([addr, generated_addr]),
        ignores=FrozenOrderedSet(),
    )

    assert epd.any_are_covered_by_includes(()) is False
    assert epd.any_are_covered_by_includes((addr,)) is True
    assert epd.any_are_covered_by_includes((generated_addr,)) is True
    assert epd.any_are_covered_by_includes((addr, generated_addr)) is True
    # Generated targets are covered if their original target generator is in the includes.
    assert (
        epd.any_are_covered_by_includes((Address("", target_name="a", generated_name="gen"),))
        is True
    )
    assert epd.any_are_covered_by_includes((Address("", target_name="x"),)) is False
    assert (
        epd.any_are_covered_by_includes((Address("", target_name="x", generated_name="gen"),))
        is False
    )
    # Ensure we check for _any_, not _all_.
    assert epd.any_are_covered_by_includes((Address("", target_name="x"), addr)) is True


def test_explicitly_provided_dependencies_remaining_after_disambiguation() -> None:
    # First check disambiguation via ignores (`!` and `!!`).
    addr = Address("", target_name="a")
    generated_addr = Address("", target_name="b", generated_name="gen")
    epd = ExplicitlyProvidedDependencies(
        Address("", target_name="input_tgt"),
        includes=FrozenOrderedSet(),
        ignores=FrozenOrderedSet([addr, generated_addr]),
    )

    def assert_disambiguated_via_ignores(ambiguous: List[Address], expected: Set[Address]) -> None:
        assert (
            epd.remaining_after_disambiguation(tuple(ambiguous), owners_must_be_ancestors=False)
            == expected
        )

    assert_disambiguated_via_ignores([], set())
    assert_disambiguated_via_ignores([addr], set())
    assert_disambiguated_via_ignores([generated_addr], set())
    assert_disambiguated_via_ignores([addr, generated_addr], set())
    # Generated targets are covered if their original target generator is in the ignores.
    assert_disambiguated_via_ignores([Address("", target_name="a", generated_name="gen")], set())

    bad_tgt = Address("", target_name="x")
    bad_generated_tgt = Address("", target_name="x", generated_name="gen")
    assert_disambiguated_via_ignores([bad_tgt], {bad_tgt})
    assert_disambiguated_via_ignores([bad_generated_tgt], {bad_generated_tgt})
    assert_disambiguated_via_ignores([bad_generated_tgt, addr, generated_addr], {bad_generated_tgt})

    # Check disambiguation via `owners_must_be_ancestors`.
    epd = ExplicitlyProvidedDependencies(
        Address("src/lang/project"), FrozenOrderedSet(), FrozenOrderedSet()
    )
    valid_candidates = {
        Address("src/lang/project", target_name="another_tgt"),
        Address("src/lang"),
        Address("src"),
        Address("", target_name="root_owner"),
    }
    invalid_candidates = {
        Address("tests/lang"),
        Address("src/another_lang"),
        Address("src/lang/another_project"),
        Address("src/lang/project/subdir"),
    }
    assert (
        epd.remaining_after_disambiguation(
            (*valid_candidates, *invalid_candidates), owners_must_be_ancestors=True
        )
        == valid_candidates
    )


def test_explicitly_provided_dependencies_disambiguated() -> None:
    def get_disambiguated(
        ambiguous: List[Address],
        *,
        ignores: Optional[List[Address]] = None,
        includes: Optional[List[Address]] = None,
        owners_must_be_ancestors: bool = False,
    ) -> Optional[Address]:
        epd = ExplicitlyProvidedDependencies(
            address=Address("dir", target_name="input_tgt"),
            includes=FrozenOrderedSet(includes or []),
            ignores=FrozenOrderedSet(ignores or []),
        )
        return epd.disambiguated(
            tuple(ambiguous), owners_must_be_ancestors=owners_must_be_ancestors
        )

    # A mix of normal and generated addresses.
    addr_a = Address("dir", target_name="a", generated_name="gen")
    addr_b = Address("dir", target_name="b", generated_name="gen")
    addr_c = Address("dir", target_name="c")
    all_addr = [addr_a, addr_b, addr_c]

    # If 1 target remains, it's disambiguated. Note that ignores can be normal or generated targets.
    assert get_disambiguated(all_addr, ignores=[addr_b, addr_c]) == addr_a
    assert (
        get_disambiguated(all_addr, ignores=[addr_b.maybe_convert_to_target_generator(), addr_c])
        == addr_a
    )

    assert get_disambiguated(all_addr, ignores=[addr_a]) is None
    assert get_disambiguated(all_addr, ignores=[addr_a.maybe_convert_to_target_generator()]) is None
    assert get_disambiguated(all_addr, ignores=all_addr) is None
    assert get_disambiguated([]) is None
    # If any includes would disambiguate the ambiguous target, we don't consider disambiguating
    # via excludes as the user has already explicitly disambiguated the module.
    assert get_disambiguated(all_addr, ignores=[addr_a, addr_b], includes=[addr_a]) is None
    assert (
        get_disambiguated(
            ambiguous=all_addr,
            ignores=[addr_a, addr_b],
            includes=[addr_a.maybe_convert_to_target_generator()],
        )
        is None
    )

    # You can also disambiguate via `owners_must_be_ancestors`.
    another_dir = Address("another_dir")
    assert get_disambiguated([addr_a, another_dir], owners_must_be_ancestors=True) == addr_a
    assert get_disambiguated([addr_a, another_dir], owners_must_be_ancestors=False) is None
    assert (
        get_disambiguated(
            [addr_a, addr_b, another_dir], ignores=[addr_b], owners_must_be_ancestors=True
        )
        == addr_a
    )


def test_explicitly_provided_dependencies_maybe_warn_of_ambiguous_dependency_inference(
    caplog,
) -> None:
    def maybe_warn(
        ambiguous: List[Address],
        *,
        ignores: Optional[List[Address]] = None,
        includes: Optional[List[Address]] = None,
        owners_must_be_ancestors: bool = False,
    ) -> None:
        caplog.clear()
        epd = ExplicitlyProvidedDependencies(
            Address("dir", target_name="input_tgt"),
            includes=FrozenOrderedSet(includes or []),
            ignores=FrozenOrderedSet(ignores or []),
        )
        epd.maybe_warn_of_ambiguous_dependency_inference(
            tuple(ambiguous),
            Address("some_dir"),
            import_reference="file",
            context="foo",
            owners_must_be_ancestors=owners_must_be_ancestors,
        )

    maybe_warn([])
    assert not caplog.records

    # A mix of normal and generated addresses.
    addr_a = Address("dir", target_name="a", generated_name="gen")
    addr_b = Address("dir", target_name="b", generated_name="gen")
    addr_c = Address("dir", target_name="c")
    all_addr = [addr_a, addr_b, addr_c]

    maybe_warn(all_addr)
    assert len(caplog.records) == 1
    assert f"['{addr_a}', '{addr_b}', '{addr_c}']" in caplog.text

    # Ignored addresses do not show up in the list of ambiguous owners, including for ignores of
    # both file and BUILD targets.
    maybe_warn(all_addr, ignores=[addr_b])
    assert len(caplog.records) == 1
    assert f"['{addr_a}', '{addr_c}']" in caplog.text
    maybe_warn(all_addr, ignores=[addr_b.maybe_convert_to_target_generator()])
    assert len(caplog.records) == 1
    assert f"['{addr_a}', '{addr_c}']" in caplog.text

    # Disambiguating via ignores turns off the warning, including for ignores of both normal and
    # generated targets.
    maybe_warn(all_addr, ignores=[addr_a, addr_b])
    assert not caplog.records
    maybe_warn(
        all_addr,
        ignores=[
            addr_a.maybe_convert_to_target_generator(),
            addr_b.maybe_convert_to_target_generator(),
        ],
    )
    assert not caplog.records

    # Including a target turns off the warning, including for includes of both normal and generated
    # targets.
    maybe_warn(all_addr, includes=[addr_a])
    assert not caplog.records
    maybe_warn(all_addr, includes=[addr_a.maybe_convert_to_target_generator()])
    assert not caplog.records

    # You can also disambiguate via `owners_must_be_ancestors`.
    another_dir = Address("another_dir")
    maybe_warn([addr_a, another_dir], owners_must_be_ancestors=True)
    assert not caplog.records
    maybe_warn([addr_a, another_dir], owners_must_be_ancestors=False)
    assert len(caplog.records) == 1
    assert f"['{another_dir}', '{addr_a}']" in caplog.text
    maybe_warn([addr_a, addr_b, another_dir], ignores=[addr_b], owners_must_be_ancestors=True)
    assert not caplog.records


# -----------------------------------------------------------------------------------------------
# Test `overrides` field
# -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_value",
    [
        0,
        object(),
        "hello",
        ["hello"],
        ["hello", "world"],
        {"hello": 0},
        {0: "world"},
        {"hello": "world"},
        {("hello",): "world"},
        {("hello",): ["world"]},
        {(0,): {"field": "value"}},
        {("hello",): {0: "value"}},
    ],
)
def test_overrides_field_data_validation(raw_value: Any) -> None:
    with pytest.raises(InvalidFieldTypeException):
        OverridesField(raw_value, Address("", target_name="example"))


def test_overrides_field_normalization() -> None:
    addr = Address("", target_name="example")

    assert OverridesField(None, addr).value is None
    assert OverridesField({}, addr).value == {}

    # Note that `list_field` is not hashable. We have to override `__hash__` for this to work.
    tgt1_override = {"str_field": "value", "list_field": [0, 1, 3]}
    tgt2_override = {"int_field": 0, "dict_field": {"a": 0}}

    # Convert a `str` key to `tuple[str, ...]`.
    field = OverridesField({"tgt1": tgt1_override, ("tgt1", "tgt2"): tgt2_override}, addr)
    assert field.value == {("tgt1",): tgt1_override, ("tgt1", "tgt2"): tgt2_override}
    with no_exception():
        hash(field)

    path_field = OverridesField(
        {"foo.ext": tgt1_override, ("foo.ext", "bar*.ext"): tgt2_override}, Address("dir")
    )
    to_globs = [
        path_globs.globs for path_globs in path_field.to_path_globs(FilesNotFoundBehavior.error)
    ]
    assert to_globs == [("dir/foo.ext",), ("dir/bar*.ext", "dir/foo.ext")]
    assert path_field.flatten_paths(
        {
            Paths(("dir/foo.ext",), ()): tgt1_override,
            Paths(("dir/bar1.ext", "dir/bar2.ext"), ()): tgt2_override,
        },
    ) == {
        "dir/foo.ext": tgt1_override,
        "dir/bar1.ext": tgt2_override,
        "dir/bar2.ext": tgt2_override,
    }
    assert path_field.flatten() == {
        "foo.ext": {**tgt1_override, **tgt2_override},
        "bar*.ext": tgt2_override,
    }
    with pytest.raises(InvalidFieldException):
        # Same field is overridden for the same file multiple times, which is an error.
        path_field.flatten_paths(
            {
                Paths(("dir/foo.ext",), ()): tgt1_override,
                Paths(("dir/foo.ext", "dir/bar.ext"), ()): tgt1_override,
            }
        )
