"""openaxpp300s: AX PP300S buck-boost bench power supply (AxinDIY) over its USB serial port.

Modules:
  cli        - the `openaxpp300s` command line (JSON output)
  mcp_server - MCP server (`openaxpp300s-mcp`) for AI agents
  protocol   - frame format, register table, value conversion, reply decoder (pure, no I/O)
  transport  - serial port discovery, per-transaction port sessions, retries
  device     - PP300S: status, settings with safe ordering, output control, readback checks
  state      - files under state/: last values sent, bench limits, stop requests, running jobs
  runner     - long-running loops: watch, log, sequence
  sim        - virtual PP300S (tests, CI, trying the tool without hardware)
Run the CLI with `python -m openaxpp300s --help`.
"""
__version__ = "0.1.0"
