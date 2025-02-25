# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

# NB: The project names must follow the naming scheme at
#  https://www.python.org/dev/peps/pep-0503/#normalized-names.
DEFAULT_MODULE_MAPPING = {
    "ansicolors": ("colors",),
    "apache-airflow": ("airflow",),
    "attrs": ("attr",),
    # Azure
    "azure-common": ("azure.common",),
    "azure-core": ("azure.core",),
    "azure-graphrbac": ("azure.graphrbac",),
    "azure-identity": ("azure.identity",),
    "azure-keyvault-certificates": ("azure.keyvault.certificates",),
    "azure-keyvault-keys": ("azure.keyvault.keys",),
    "azure-keyvault-secrets": ("azure.keyvault.secrets",),
    "azure-keyvault": ("azure.keyvault",),
    "azure-mgmt-apimanagement": ("azure.mgmt.apimanagement",),
    "azure-mgmt-authorization": ("azure.mgmt.authorization",),
    "azure-mgmt-automation": ("azure.mgmt.automation",),
    "azure-mgmt-batch": ("azure.mgmt.batch",),
    "azure-mgmt-compute": ("azure.mgmt.compute",),
    "azure-mgmt-containerinstance": ("azure.mgmt.containerinstance",),
    "azure-mgmt-containerregistry": ("azure.mgmt.containerregistry",),
    "azure-mgmt-containerservice": ("azure.mgmt.containerservice",),
    "azure-mgmt-core": ("azure.mgmt.core",),
    "azure-mgmt-cosmosdb": ("azure.mgmt.cosmosdb",),
    "azure-mgmt-frontdoor": ("azure.mgmt.frontdoor",),
    "azure-mgmt-hybridkubernetes": ("azure.mgmt.hybridkubernetes",),
    "azure-mgmt-keyvault": ("azure.mgmt.keyvault",),
    "azure-mgmt-logic": ("azure.mgmt.logic",),
    "azure-mgmt-managementgroups": ("azure.mgmt.managementgroups",),
    "azure-mgmt-monitor": ("azure.mgmt.monitor",),
    "azure-mgmt-msi": ("azure.mgmt.msi",),
    "azure-mgmt-network": ("azure.mgmt.network",),
    "azure-mgmt-rdbms": ("azure.mgmt.rdbms",),
    "azure-mgmt-resource": ("azure.mgmt.resource",),
    "azure-mgmt-security": ("azure.mgmt.security",),
    "azure-mgmt-servicefabric": ("azure.mgmt.servicefabric",),
    "azure-mgmt-sql": ("azure.mgmt.sql",),
    "azure-mgmt-storage": ("azure.mgmt.storage",),
    "azure-mgmt-subscription": ("azure.mgmt.subscription",),
    "azure-mgmt-web": ("azure.mgmt.web",),
    "azure-storage-blob": ("azure.storage.blob",),
    "azure-storage-queue": ("azure.storage.queue",),
    "beautifulsoup4": ("bs4",),
    "bitvector": ("BitVector",),
    "django-cors-headers": ("corsheaders",),
    "django-debug-toolbar": ("debug_toolbar",),
    "django-filter": ("django_filters",),
    "django-simple-history": ("simple_history",),
    "djangorestframework": ("rest_framework",),
    "enum34": ("enum",),
    # See https://github.com/googleapis/google-cloud-python#libraries for all Google cloud
    # libraries. We only add libraries in GA, not beta.
    "google-cloud-aiplatform": ("google.cloud.aiplatform",),
    "google-cloud-bigquery": ("google.cloud.bigquery",),
    "google-cloud-bigtable": ("google.cloud.bigtable",),
    "google-cloud-datastore": ("google.cloud.datastore",),
    "google-cloud-firestore": ("google.cloud.firestore",),
    "google-cloud-functions": ("google.cloud.functions_v1", "google.cloud.functions"),
    "google-cloud-iam": ("google.cloud.iam_credentials_v1",),
    "google-cloud-iot": ("google.cloud.iot_v1",),
    "google-cloud-logging": ("google.cloud.logging_v2", "google.cloud.logging"),
    "google-cloud-pubsub": ("google.cloud.pubsub_v1", "google.cloud.pubsub"),
    "google-cloud-secret-manager": ("google.cloud.secretmanager",),
    "google-cloud-storage": ("google.cloud.storage",),
    "opencv-python": ("cv2",),
    "paho-mqtt": ("paho",),
    "pillow": ("PIL",),
    "psycopg2-binary": ("psycopg2",),
    "protobuf": ("google.protobuf",),
    "pycrypto": ("Crypto",),
    "pyopenssl": ("OpenSSL",),
    "pypdf2": ("PyPDF2",),
    "python-dateutil": ("dateutil",),
    "python-docx": ("docx",),
    "python-dotenv": ("dotenv",),
    "python-hcl2": ("hcl2",),
    "python-jose": ("jose",),
    "python-pptx": ("pptx",),
    "pyyaml": ("yaml",),
    "pymongo": ("bson", "gridfs"),
    "pymupdf": ("fitz",),
    "pytest-runner": ("ptr",),
    "scikit-image": ("skimage",),
    "scikit-learn": ("sklearn",),
    "setuptools": ("easy_install", "pkg_resources", "setuptools"),
}

DEFAULT_TYPE_STUB_MODULE_MAPPING = {
    "djangorestframework-types": ("rest_framework",),
    "types-enum34": ("enum34",),
    "types-pillow": ("PIL",),
    "types-protobuf": ("google.protobuf",),
    "types-pycrypto": ("Crypto",),
    "types-pyopenssl": ("OpenSSL",),
    "types-pyyaml": ("yaml",),
    "types-python-dateutil": ("dateutil",),
    "types-setuptools": ("easy_install", "pkg_resources", "setuptools"),
}
