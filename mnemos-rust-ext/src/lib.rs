use ndarray::ArrayView1;
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::PySequence;
use wide::f32x8;

pub mod federation;

const LANES: usize = 8;

fn horizontal_sum(values: f32x8) -> f32 {
    let lanes: [f32; LANES] = values.into();
    lanes.iter().sum()
}

fn dot_norms_simd(a: &[f32], b: &[f32]) -> (f32, f32, f32) {
    let mut dot_acc = f32x8::splat(0.0);
    let mut norm_a_acc = f32x8::splat(0.0);
    let mut norm_b_acc = f32x8::splat(0.0);

    let chunks = a.len() / LANES;
    for idx in 0..chunks {
        let start = idx * LANES;
        let va = f32x8::from([
            a[start],
            a[start + 1],
            a[start + 2],
            a[start + 3],
            a[start + 4],
            a[start + 5],
            a[start + 6],
            a[start + 7],
        ]);
        let vb = f32x8::from([
            b[start],
            b[start + 1],
            b[start + 2],
            b[start + 3],
            b[start + 4],
            b[start + 5],
            b[start + 6],
            b[start + 7],
        ]);
        dot_acc += va * vb;
        norm_a_acc += va * va;
        norm_b_acc += vb * vb;
    }

    let mut dot = horizontal_sum(dot_acc);
    let mut norm_a = horizontal_sum(norm_a_acc);
    let mut norm_b = horizontal_sum(norm_b_acc);

    for idx in (chunks * LANES)..a.len() {
        let left = a[idx];
        let right = b[idx];
        dot += left * right;
        norm_a += left * left;
        norm_b += right * right;
    }

    (dot, norm_a, norm_b)
}

fn dot_norms_scalar(a: &[f32], b: &[f32]) -> (f32, f32, f32) {
    let left = ArrayView1::from(a);
    let right = ArrayView1::from(b);
    let dot = left.dot(&right);
    let norm_a = left.dot(&left);
    let norm_b = right.dot(&right);
    (dot, norm_a, norm_b)
}

fn norm_simd(values: &[f32]) -> f32 {
    if values.is_empty() {
        return 0.0;
    }
    let mut norm_acc = f32x8::splat(0.0);
    let chunks = values.len() / LANES;
    for idx in 0..chunks {
        let start = idx * LANES;
        let vector = f32x8::from([
            values[start],
            values[start + 1],
            values[start + 2],
            values[start + 3],
            values[start + 4],
            values[start + 5],
            values[start + 6],
            values[start + 7],
        ]);
        norm_acc += vector * vector;
    }

    let mut norm = horizontal_sum(norm_acc);
    for value in &values[(chunks * LANES)..] {
        norm += value * value;
    }
    norm
}

fn dot_and_norm_right_simd(a: &[f32], b: &[f32]) -> (f32, f32) {
    let mut dot_acc = f32x8::splat(0.0);
    let mut norm_b_acc = f32x8::splat(0.0);

    let chunks = a.len() / LANES;
    for idx in 0..chunks {
        let start = idx * LANES;
        let va = f32x8::from([
            a[start],
            a[start + 1],
            a[start + 2],
            a[start + 3],
            a[start + 4],
            a[start + 5],
            a[start + 6],
            a[start + 7],
        ]);
        let vb = f32x8::from([
            b[start],
            b[start + 1],
            b[start + 2],
            b[start + 3],
            b[start + 4],
            b[start + 5],
            b[start + 6],
            b[start + 7],
        ]);
        dot_acc += va * vb;
        norm_b_acc += vb * vb;
    }

    let mut dot = horizontal_sum(dot_acc);
    let mut norm_b = horizontal_sum(norm_b_acc);
    for idx in (chunks * LANES)..a.len() {
        let left = a[idx];
        let right = b[idx];
        dot += left * right;
        norm_b += right * right;
    }
    (dot, norm_b)
}

pub fn cosine_similarity_impl(a: &[f32], b: &[f32]) -> f32 {
    if a.is_empty() || b.is_empty() || a.len() != b.len() {
        return 0.0;
    }

    let (dot, norm_a, norm_b) = if a.len() >= LANES {
        dot_norms_simd(a, b)
    } else {
        dot_norms_scalar(a, b)
    };

    if norm_a == 0.0 || norm_b == 0.0 {
        return 0.0;
    }
    dot / (norm_a.sqrt() * norm_b.sqrt())
}

pub fn batch_cosine_similarity_impl(query: &[f32], corpus: &[Vec<f32>]) -> Vec<f32> {
    if query.is_empty() {
        return vec![0.0; corpus.len()];
    }

    let norm_query = norm_simd(query);
    if norm_query == 0.0 {
        return vec![0.0; corpus.len()];
    }
    let norm_query_sqrt = norm_query.sqrt();

    corpus
        .iter()
        .map(|candidate| {
            if candidate.len() != query.len() || candidate.is_empty() {
                return 0.0;
            }
            let (dot, norm_candidate) = if candidate.len() >= LANES {
                dot_and_norm_right_simd(query, candidate)
            } else {
                let query_view = ArrayView1::from(query);
                let candidate_view = ArrayView1::from(candidate.as_slice());
                (
                    query_view.dot(&candidate_view),
                    candidate_view.dot(&candidate_view),
                )
            };
            if norm_candidate == 0.0 {
                0.0
            } else {
                dot / (norm_query_sqrt * norm_candidate.sqrt())
            }
        })
        .collect()
}

#[pyfunction]
fn cosine_similarity(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<f32> {
    if let (Ok(a_array), Ok(b_array)) = (
        a.extract::<PyReadonlyArray1<f32>>(),
        b.extract::<PyReadonlyArray1<f32>>(),
    ) {
        if let (Ok(a_slice), Ok(b_slice)) = (a_array.as_slice(), b_array.as_slice()) {
            return Ok(cosine_similarity_impl(a_slice, b_slice));
        }
    }

    let a = a.extract::<Vec<f32>>()?;
    let b = b.extract::<Vec<f32>>()?;
    Ok(cosine_similarity_impl(&a, &b))
}

#[pyfunction]
fn batch_cosine_similarity(
    query: &Bound<'_, PyAny>,
    corpus: &Bound<'_, PyAny>,
) -> PyResult<Vec<f32>> {
    if let (Ok(query_array), Ok(corpus_array)) = (
        query.extract::<PyReadonlyArray1<f32>>(),
        corpus.extract::<PyReadonlyArray2<f32>>(),
    ) {
        if let Ok(query_slice) = query_array.as_slice() {
            let corpus_view = corpus_array.as_array();
            if query_slice.is_empty() {
                return Ok(vec![0.0; corpus_view.nrows()]);
            }

            let norm_query = norm_simd(query_slice);
            if norm_query == 0.0 {
                return Ok(vec![0.0; corpus_view.nrows()]);
            }
            let norm_query_sqrt = norm_query.sqrt();
            let mut scores = Vec::with_capacity(corpus_view.nrows());

            for row in corpus_view.outer_iter() {
                if row.len() != query_slice.len() || row.is_empty() {
                    scores.push(0.0);
                    continue;
                }
                if let Some(row_slice) = row.as_slice() {
                    let (dot, norm_candidate) = if row_slice.len() >= LANES {
                        dot_and_norm_right_simd(query_slice, row_slice)
                    } else {
                        let query_view = ArrayView1::from(query_slice);
                        let candidate_view = ArrayView1::from(row_slice);
                        (
                            query_view.dot(&candidate_view),
                            candidate_view.dot(&candidate_view),
                        )
                    };
                    if norm_candidate == 0.0 {
                        scores.push(0.0);
                    } else {
                        scores.push(dot / (norm_query_sqrt * norm_candidate.sqrt()));
                    }
                } else {
                    let candidate = row.to_vec();
                    scores.push(cosine_similarity_impl(query_slice, &candidate));
                }
            }
            return Ok(scores);
        }
    }

    let query = query.extract::<Vec<f32>>()?;
    if query.is_empty() {
        let corpus_seq = corpus.downcast::<PySequence>()?;
        return Ok(vec![0.0; corpus_seq.len()?]);
    }

    let norm_query = norm_simd(&query);
    let corpus_seq = corpus
        .downcast::<PySequence>()
        .map_err(|_| PyTypeError::new_err("corpus must be a sequence of float sequences"))?;
    let corpus_len = corpus_seq.len()?;
    if norm_query == 0.0 {
        return Ok(vec![0.0; corpus_len]);
    }
    let norm_query_sqrt = norm_query.sqrt();
    let mut scores = Vec::with_capacity(corpus_len);

    for idx in 0..corpus_len {
        let candidate_obj = corpus_seq.get_item(idx)?;
        let candidate = candidate_obj.extract::<Vec<f32>>()?;
        if candidate.len() != query.len() || candidate.is_empty() {
            scores.push(0.0);
            continue;
        }

        let (dot, norm_candidate) = if candidate.len() >= LANES {
            dot_and_norm_right_simd(&query, &candidate)
        } else {
            let query_view = ArrayView1::from(query.as_slice());
            let candidate_view = ArrayView1::from(candidate.as_slice());
            (
                query_view.dot(&candidate_view),
                candidate_view.dot(&candidate_view),
            )
        };
        if norm_candidate == 0.0 {
            scores.push(0.0);
        } else {
            scores.push(dot / (norm_query_sqrt * norm_candidate.sqrt()));
        }
    }
    Ok(scores)
}

#[pyfunction]
fn cosine(a: &Bound<'_, PyAny>, b: &Bound<'_, PyAny>) -> PyResult<f32> {
    cosine_similarity(a, b)
}

#[pyfunction]
fn cosine_batch(query: &Bound<'_, PyAny>, corpus: &Bound<'_, PyAny>) -> PyResult<Vec<f32>> {
    batch_cosine_similarity(query, corpus)
}

#[pymodule]
fn mnemos_native_search(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(cosine_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(batch_cosine_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(cosine, m)?)?;
    m.add_function(wrap_pyfunction!(cosine_batch, m)?)?;
    m.add_function(wrap_pyfunction!(federation::serialize_memory_for_feed, m)?)?;
    m.add_function(wrap_pyfunction!(federation::serialize_memory_rows, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{batch_cosine_similarity_impl, cosine_similarity_impl};

    #[test]
    fn cosine_handles_basic_geometry() {
        assert!((cosine_similarity_impl(&[1.0, 0.0], &[1.0, 0.0]) - 1.0).abs() < 1e-6);
        assert!(cosine_similarity_impl(&[1.0, 0.0], &[0.0, 1.0]).abs() < 1e-6);
        assert!((cosine_similarity_impl(&[1.0, 0.0], &[-1.0, 0.0]) + 1.0).abs() < 1e-6);
    }

    #[test]
    fn cosine_returns_zero_for_invalid_inputs() {
        assert_eq!(cosine_similarity_impl(&[], &[1.0]), 0.0);
        assert_eq!(cosine_similarity_impl(&[1.0], &[1.0, 2.0]), 0.0);
        assert_eq!(cosine_similarity_impl(&[0.0, 0.0], &[1.0, 2.0]), 0.0);
    }

    #[test]
    fn simd_path_matches_reference() {
        let a: Vec<f32> = (0..384).map(|idx| ((idx + 1) as f32).sin()).collect();
        let b: Vec<f32> = (0..384).map(|idx| ((idx + 7) as f32).cos()).collect();
        let dot: f32 = a
            .iter()
            .zip(b.iter())
            .map(|(left, right)| left * right)
            .sum();
        let norm_a: f32 = a.iter().map(|value| value * value).sum::<f32>().sqrt();
        let norm_b: f32 = b.iter().map(|value| value * value).sum::<f32>().sqrt();
        let expected = dot / (norm_a * norm_b);

        assert!((cosine_similarity_impl(&a, &b) - expected).abs() < 1e-6);
    }

    #[test]
    fn batch_scores_all_candidates() {
        let scores = batch_cosine_similarity_impl(
            &[1.0, 0.0],
            &[vec![1.0, 0.0], vec![0.0, 1.0], vec![-1.0, 0.0]],
        );
        assert_eq!(scores.len(), 3);
        assert!((scores[0] - 1.0).abs() < 1e-6);
        assert!(scores[1].abs() < 1e-6);
        assert!((scores[2] + 1.0).abs() < 1e-6);
    }
}
