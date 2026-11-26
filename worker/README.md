# Trusted local measurement worker

Run the first in-memory gzip profile with Python 3.10 or later on a POSIX host.

```sh
python3 -B worker/measure.py
python3 -B worker/measure.py --timeout 5 path/to/an-input
python3 -B -m unittest -v worker.test_measure
```

The first command uses a fixed text fixture. The second reads a regular input file up to 1 MiB. Each run launches a fresh process for one gzip level-6 encode using one thread. The command prints the platform, clock source and nominal clock resolution, elapsed nanoseconds, byte counts and validation result. Exit 0 means the observation qualified locally. Exit 1 reports an unqualified or failed trial, exit 2 a command-line error, and exit 130 an interrupted trial.

The worker loads the input before the first `perf_counter_ns()` reading. The timed interval includes allocating the output sink, creating a fresh encoder, writing, closing the stream to flush the data and trailer, and obtaining the complete artifact bytes. Process startup, input and report I/O, and whole-artifact decode and byte comparison are outside the interval. A positive interval and exact reconstruction are both required for local qualification. A measured mismatch retains its byte counts and elapsed time. Zero elapsed time is reported as resolution-limited.

The supervisor stages input in an exclusive temporary directory, launches the worker as a separate process group, and applies the timeout while waiting for exit. On timeout or Ctrl-C it kills that group and waits for the worker before cleaning the directory. It discards output from a timed-out or abnormally terminated child. The built-in worker emits a single diagnostic line and the supervisor reads at most 4 KiB of it. This path is for trusted local code. Resource isolation for arbitrary adapters and the stable evidence bundle have their own specification work.
