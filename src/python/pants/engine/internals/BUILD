# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources(
    overrides={"native_engine.pyi": {"dependencies": [":native_engine"]}},
)

python_tests(name="tests", sources=["*_test.py", "!scheduler_integration_test.py"], timeout=90)

python_tests(
    name="scheduler_integration_test",
    sources=["scheduler_integration_test.py"],
    dependencies=[
        "pants-plugins/internal_plugins/rules_for_testing",
        "testprojects/src/python:hello_directory",
    ],
    timeout=180,
)

resources(
    name="native_engine",
    sources=["native_engine.so", "native_engine.so.metadata"],
)

resources(name="fs_test_data", sources=["fs_test_data/fs_test.tar", "fs_test_data/tls/rsa/*"])
