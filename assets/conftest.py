# assets/app/ is the template install.py copies to system/apps/context/, test files and all.
# Collecting both copies in one root run is an import-file basename collision, so the tests
# run from the installed copy.
collect_ignore = ["app"]
