# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_requirement(name="badreq", requirements=["badreq==99.99.99"])

pex_binary(
    name="use_badreq",
    dependencies=[":badreq"],
    entry_point="bad_requirements.use_badreq",
    tags=["lockfile_ignore"],
)
