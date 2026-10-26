"""Allow the original module-install RPC to finish on a development host."""
import runpy
import sys

from odoolib import json2

json2.DEFAULT_TIMEOUT = 900
sys.argv = ["/setup/setup_scenario.original.py"]
runpy.run_path(sys.argv[0], run_name="__main__")
