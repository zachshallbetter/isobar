# Trusted local measurement worker

Run the first in-memory gzip profile with Python 3.10 or later on a POSIX host.

```sh
python3 -B worker/measure.py
python3 -B worker/measure.py --timeout 5 path/to/an-input
python3 -B -m unittest -v worker.test_measure
```

The first command uses a fixed text fixture. The second reads a regular input file up to 1 MiB. Each run launches a fresh process for one gzip level-6 encode using one thread. The command prints the platform, clock source and nominal clock resolution, elapsed nanoseconds, byte counts and validation result. Exit 0 means the observation qualified locally. Exit 1 reports an unqualified or failed trial, exit 2 a command-line error, and exit 130 an interrupted trial.

The worker loads the input before the first `perf_counter_ns()` reading. The timed interval includes allocating the output sink, creating a fresh encoder, writing, closing the stream to flush the data and trailer, and obtaining the complete artifact bytes. Process startup, input and report I/O, and whole-artifact decode and byte comparison are outside the interval. The gzip verifier checks each member and its trailer before treating the output as complete. A positive interval and exact reconstruction are both required for local qualification. A measured mismatch retains its byte counts and elapsed time. Zero elapsed time is reported as resolution-limited.

The supervisor stages input in an exclusive temporary directory, launches the worker as a separate process group, and applies the timeout while waiting for exit. On timeout or Ctrl-C it kills that group and waits for the worker before cleaning the directory. It discards output from a timed-out or abnormally terminated child. The built-in worker sends a private result to the supervisor, which reads at most 4 KiB. This path is for trusted local code. Resource isolation for arbitrary adapters and the stable evidence bundle have their own specification work.

To emit the draft observation and validation records, supply a unique opaque run ID for each invocation. Set up the verifier using the [conformance instructions](../conformance/README.md).

```sh
python3 -B worker/measure.py --evidence example-1 > /tmp/isobar-example.jsonl
/tmp/isobar-conformance/bin/python -B conformance/verify_evidence.py /tmp/isobar-example.jsonl
```

Evidence mode writes only JSON Lines to stdout. Use `--warmup` with `--evidence` to retain a warm-up as an unqualified record. An exit of 1 can still produce evidence, including a timeout, mismatch or zero-duration observation. The verifier reports validity separately from qualification.
