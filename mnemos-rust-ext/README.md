# mnemos-native-search

PyO3 Rust extension for MNEMOS vector similarity and federation feed
serialization hot paths.

## Development build

```bash
cd mnemos-rust-ext
maturin develop
```

This installs the `mnemos_native_search` Python module into the active virtualenv.

## Release build

```bash
cd mnemos-rust-ext
maturin build --release
python -m pip install target/wheels/mnemos_native_search-*.whl
```

For production hosts, build on the same target architecture you deploy to, or build
one wheel per architecture.

## API

```python
import mnemos_native_search

score = mnemos_native_search.cosine_similarity([1.0, 0.0], [0.5, 0.5])
scores = mnemos_native_search.batch_cosine_similarity(
    [1.0, 0.0],
    [[1.0, 0.0], [0.0, 1.0]],
)
```

The same functions accept contiguous NumPy `float32` arrays for lower-overhead
batch scoring:

```python
import numpy as np

query = np.asarray([1.0, 0.0], dtype=np.float32)
corpus = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
scores = mnemos_native_search.batch_cosine_similarity(query, corpus)
```

Compatibility aliases are also exported:

- `cosine(...)`
- `cosine_batch(...)`

Federation feed rows can be serialized directly to compact JSON bytes:

```python
payload = mnemos_native_search.serialize_memory_rows(rows)
```
