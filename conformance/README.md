# Experimental conformance vectors

These public test vectors exercise the draft measurement, evidence and scoring rules along with a small set of gzip and Zstandard artifact cases. The checker is a reference for these cases. It is not an evidence-bundle verifier, production decoder, benchmark runner or normative scoring profile.

From the repository root, use Python 3.10 or later and Zstandard CLI 1.5.7. The tested reference CLI was built at [f8745da6](https://github.com/facebook/zstd/tree/f8745da6ff1ad1e7bab384bd1f9d742439278e99) with `make -C programs zstd HAVE_ZLIB=0 HAVE_LZMA=0 HAVE_LZ4=0`.

```sh
python3 -m venv /tmp/isobar-conformance
/tmp/isobar-conformance/bin/python -m pip install -r conformance/requirements.txt
/tmp/isobar-conformance/bin/python -B conformance/check.py --zstd zstd
```

Pass the absolute path to the built binary with `--zstd` if it is not on PATH. The [vector file](vectors.json) stores fixed artifact bytes and exact expected output. Resetting, byte accounting and timing across platforms require additional adapter tests. For the metric examples, numeric values use a relative comparison tolerance of `1e-12`, and the prototype uses an absolute tolerance of `1e-12` on the weight sum. These tolerances apply only to this executable proposal.

Structural schema checks, semantic reference checks and measurement qualification are separate assertions. The measurement records use opaque fixture identifiers. They do not define a run identity or a hash canonicalization scheme.
