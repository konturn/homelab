from hypothesis import settings

# No example database in the repo checkout; CI runs are reproducible from the seed.
settings.register_profile("router", database=None, derandomize=False)
settings.load_profile("router")
