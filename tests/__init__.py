import logging

# The modules log warnings on purpose (cancelled batches, stale locks...). Keep test output clean.
logging.getLogger("reliable_ops").setLevel(logging.ERROR)
