"""Internal smoke/verify script (not part of the package). Run via uv."""
import sys

from src import config

print("PYTHON:", sys.executable)
sc = config.load_scenarios()
eu = config.load_entity_universe()
print("scenarios:", list(sc.keys()))
print("hyperscalers:", [h["name"] for h in eu["hyperscalers"]])
print("pc_outstanding:", config.assumption_value("private_credit_ai_datacenter_outstanding"))

import pandas
import pydantic

print("deps OK pandas", pandas.__version__, "pydantic", pydantic.__version__)
