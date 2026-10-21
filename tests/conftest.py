# Load qwb first so its import-order guard (torch -> osqp -> pandas) applies
# before any test module imports pandas.
import qwb  # noqa: F401
