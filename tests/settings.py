ALLOWED_HOSTS = ["testserver"]
DEBUG = False
DEFAULT_CHARSET = "utf-8"
INSTALLED_APPS = ["django.contrib.contenttypes", "rest_framework"]
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "tests.test_django"
REST_FRAMEWORK: dict[str, object] = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}
SECRET_KEY = "test"
USE_TZ = True
