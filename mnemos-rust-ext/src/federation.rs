use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySequence, PyTuple};
use serde::Serialize;
use serde_json::{Map, Number, Value};

fn json_error(err: simd_json::Error) -> PyErr {
    PyValueError::new_err(format!("failed to serialize federation feed row: {err}"))
}

fn py_to_json(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    if value.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(value) = value.extract::<bool>() {
        return Ok(Value::Bool(value));
    }
    if let Ok(value) = value.extract::<i64>() {
        return Ok(Value::Number(value.into()));
    }
    if let Ok(value) = value.extract::<u64>() {
        return Ok(Value::Number(value.into()));
    }
    if let Ok(value) = value.extract::<f64>() {
        return Number::from_f64(value)
            .map(Value::Number)
            .ok_or_else(|| PyValueError::new_err("non-finite float is not valid JSON"));
    }
    if let Ok(value) = value.extract::<String>() {
        return Ok(Value::String(value));
    }
    if let Ok(dict) = value.downcast::<PyDict>() {
        let mut object = Map::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            let key = key.extract::<String>()?;
            object.insert(key, py_to_json(&item)?);
        }
        return Ok(Value::Object(object));
    }
    if let Ok(list) = value.downcast::<PyList>() {
        let mut out = Vec::with_capacity(list.len());
        for item in list.iter() {
            out.push(py_to_json(&item)?);
        }
        return Ok(Value::Array(out));
    }
    if let Ok(tuple) = value.downcast::<PyTuple>() {
        let mut out = Vec::with_capacity(tuple.len());
        for item in tuple.iter() {
            out.push(py_to_json(&item)?);
        }
        return Ok(Value::Array(out));
    }
    if let Ok(isoformat) = value.getattr("isoformat") {
        if isoformat.is_callable() {
            return Ok(Value::String(isoformat.call0()?.extract::<String>()?));
        }
    }
    Ok(Value::String(value.str()?.extract::<String>()?))
}

fn get_optional<'py>(
    row: &Bound<'py, PyDict>,
    keys: &[&str],
) -> PyResult<Option<Bound<'py, PyAny>>> {
    for key in keys {
        if let Some(value) = row.get_item(*key)? {
            if !value.is_none() {
                return Ok(Some(value));
            }
        }
    }
    Ok(None)
}

fn get_required<'py>(row: &Bound<'py, PyDict>, keys: &[&str]) -> PyResult<Bound<'py, PyAny>> {
    get_optional(row, keys)?
        .ok_or_else(|| PyKeyError::new_err(format!("missing required field {}", keys[0])))
}

fn get_json_or_default(row: &Bound<'_, PyDict>, keys: &[&str], default: Value) -> PyResult<Value> {
    match get_optional(row, keys)? {
        Some(value) => py_to_json(&value),
        None => Ok(default),
    }
}

#[derive(Serialize)]
struct FeedMemoryRow {
    id: Value,
    content: Value,
    category: Value,
    tags: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    embedding: Option<Value>,
    refs: Value,
    created_at: Value,
    updated_at: Value,
}

pub fn serialize_memory_row_for_feed(row: &Bound<'_, PyDict>) -> PyResult<String> {
    let feed_row = FeedMemoryRow {
        id: py_to_json(&get_required(row, &["id"])?)?,
        content: py_to_json(&get_required(row, &["content"])?)?,
        category: py_to_json(&get_required(row, &["category"])?)?,
        tags: get_json_or_default(row, &["tags"], Value::Array(Vec::new()))?,
        embedding: get_optional(row, &["embedding"])?
            .map(|embedding| py_to_json(&embedding))
            .transpose()?,
        refs: get_json_or_default(
            row,
            &["refs", "source_memory_ids", "memory_refs"],
            Value::Array(Vec::new()),
        )?,
        created_at: py_to_json(&get_required(row, &["created_at", "created"])?)?,
        updated_at: py_to_json(&get_required(row, &["updated_at", "updated"])?)?,
    };

    simd_json::to_string(&feed_row).map_err(json_error)
}

#[pyfunction]
#[pyo3(text_signature = "(rows, /)")]
pub fn serialize_memory_for_feed(rows: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let rows = rows
        .downcast::<PySequence>()
        .map_err(|_| PyTypeError::new_err("rows must be a sequence of mappings"))?;
    let row_count = rows.len()?;
    let mut out = Vec::with_capacity(row_count);
    for idx in 0..row_count {
        let row = rows.get_item(idx)?;
        let row = row
            .downcast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("rows must contain dict mappings"))?;
        out.push(serialize_memory_row_for_feed(row)?);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyDict;

    #[test]
    fn serializes_required_feed_fields_in_order() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let row = PyDict::new_bound(py);
            row.set_item("id", "mem_1").unwrap();
            row.set_item("content", "hello").unwrap();
            row.set_item("category", "projects").unwrap();
            row.set_item("tags", vec!["rust", "federation"]).unwrap();
            row.set_item("refs", vec!["mem_0"]).unwrap();
            row.set_item("created_at", "2026-05-28T12:00:00+00:00")
                .unwrap();
            row.set_item("updated_at", "2026-05-28T12:01:00+00:00")
                .unwrap();

            let encoded = serialize_memory_row_for_feed(&row).unwrap();
            assert_eq!(
                encoded,
                r#"{"id":"mem_1","content":"hello","category":"projects","tags":["rust","federation"],"refs":["mem_0"],"created_at":"2026-05-28T12:00:00+00:00","updated_at":"2026-05-28T12:01:00+00:00"}"#
            );
        });
    }

    #[test]
    fn omits_missing_embedding_and_defaults_arrays() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let row = PyDict::new_bound(py);
            row.set_item("id", "mem_2").unwrap();
            row.set_item("content", "hello").unwrap();
            row.set_item("category", "projects").unwrap();
            row.set_item("created", "2026-05-28T12:00:00+00:00")
                .unwrap();
            row.set_item("updated", "2026-05-28T12:01:00+00:00")
                .unwrap();

            let encoded = serialize_memory_row_for_feed(&row).unwrap();
            assert_eq!(
                encoded,
                r#"{"id":"mem_2","content":"hello","category":"projects","tags":[],"refs":[],"created_at":"2026-05-28T12:00:00+00:00","updated_at":"2026-05-28T12:01:00+00:00"}"#
            );
        });
    }
}
