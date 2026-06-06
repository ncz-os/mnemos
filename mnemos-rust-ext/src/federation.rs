use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PySequence, PyTuple};
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

fn get_json_or_null(row: &Bound<'_, PyDict>, keys: &[&str]) -> PyResult<Value> {
    get_json_or_default(row, keys, Value::Null)
}

fn iso_json_or_null(row: &Bound<'_, PyDict>, keys: &[&str]) -> PyResult<Value> {
    match get_optional(row, keys)? {
        Some(value) => Ok(Value::String(py_to_iso_string(&value)?)),
        None => Ok(Value::Null),
    }
}

fn py_to_iso_string(value: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(isoformat) = value.getattr("isoformat") {
        if isoformat.is_callable() {
            return isoformat.call0()?.extract::<String>();
        }
    }
    value.str()?.extract::<String>()
}

fn metadata_json(row: &Bound<'_, PyDict>) -> PyResult<Value> {
    match get_optional(row, &["metadata"])? {
        Some(value) => {
            if let Ok(raw) = value.extract::<String>() {
                if raw.is_empty() {
                    return Ok(Value::Null);
                }
                serde_json::from_str(&raw).map_err(|err| {
                    PyValueError::new_err(format!("metadata is not valid JSON: {err}"))
                })
            } else {
                py_to_json(&value)
            }
        }
        None => Ok(Value::Null),
    }
}

fn row_has_non_null(row: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    Ok(row.get_item(key)?.is_some_and(|value| !value.is_none()))
}

fn serialize_memory_item_value(row: &Bound<'_, PyDict>) -> PyResult<Value> {
    let mut object = Map::with_capacity(27);
    object.insert("id".to_string(), py_to_json(&get_required(row, &["id"])?)?);
    object.insert(
        "content".to_string(),
        py_to_json(&get_required(row, &["content"])?)?,
    );
    object.insert(
        "category".to_string(),
        py_to_json(&get_required(row, &["category"])?)?,
    );
    object.insert("subcategory".to_string(), get_json_or_null(row, &["subcategory"])?);
    object.insert(
        "created".to_string(),
        Value::String(py_to_iso_string(&get_required(row, &["created", "created_at"])?)?),
    );
    object.insert("updated".to_string(), iso_json_or_null(row, &["updated", "updated_at"])?);
    object.insert("metadata".to_string(), metadata_json(row)?);
    object.insert("quality_rating".to_string(), get_json_or_null(row, &["quality_rating"])?);
    object.insert(
        "compressed_content".to_string(),
        get_json_or_null(row, &["compressed_content"])?,
    );
    object.insert(
        "verbatim_content".to_string(),
        get_json_or_null(row, &["verbatim_content"])?,
    );
    object.insert("source".to_string(), Value::String("openclaw".to_string()));
    object.insert("owner_id".to_string(), get_json_or_null(row, &["owner_id"])?);
    object.insert("group_id".to_string(), get_json_or_null(row, &["group_id"])?);
    object.insert("namespace".to_string(), get_json_or_null(row, &["namespace"])?);
    object.insert(
        "permission_mode".to_string(),
        get_json_or_null(row, &["permission_mode"])?,
    );
    object.insert("source_model".to_string(), get_json_or_null(row, &["source_model"])?);
    object.insert(
        "source_provider".to_string(),
        get_json_or_null(row, &["source_provider"])?,
    );
    object.insert(
        "source_session".to_string(),
        get_json_or_null(row, &["source_session"])?,
    );
    object.insert("source_agent".to_string(), get_json_or_null(row, &["source_agent"])?);
    object.insert("archived_at".to_string(), iso_json_or_null(row, &["archived_at"])?);
    object.insert(
        "archived".to_string(),
        Value::Bool(row_has_non_null(row, "archived_at")?),
    );
    object.insert("embedding".to_string(), get_json_or_null(row, &["embedding"])?);
    object.insert(
        "embedding_model".to_string(),
        get_json_or_null(row, &["embedding_model"])?,
    );
    object.insert(
        "embedding_dim".to_string(),
        get_json_or_null(row, &["embedding_dim"])?,
    );
    object.insert(
        "audit_latest_entry_id".to_string(),
        get_json_or_null(row, &["audit_latest_entry_id"])?,
    );
    object.insert(
        "audit_latest_entry_hash".to_string(),
        get_json_or_null(row, &["audit_latest_entry_hash"])?,
    );
    Ok(Value::Object(object))
}

fn serialize_consolidation_value(row: &Bound<'_, PyDict>) -> PyResult<Value> {
    let mut object = Map::with_capacity(4);
    object.insert("type".to_string(), Value::String("consolidation".to_string()));
    object.insert("id".to_string(), py_to_json(&get_required(row, &["id"])?)?);
    object.insert(
        "consolidated_into".to_string(),
        py_to_json(&get_required(row, &["consolidated_into"])?)?,
    );
    object.insert(
        "consolidated_at".to_string(),
        Value::String(py_to_iso_string(&get_required(row, &["consolidated_at"])?)?),
    );
    Ok(Value::Object(object))
}

fn serialize_feed_row_value(row: &Bound<'_, PyDict>) -> PyResult<Value> {
    if let Some(item_type) = row.get_item("type")? {
        if !item_type.is_none() && item_type.extract::<String>()? == "consolidation" {
            return serialize_consolidation_value(row);
        }
    }
    serialize_memory_item_value(row)
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

#[pyfunction]
#[pyo3(text_signature = "(rows, /)")]
pub fn serialize_memory_rows<'py>(
    py: Python<'py>,
    rows: &Bound<'py, PyAny>,
) -> PyResult<Py<PyBytes>> {
    let rows = rows
        .downcast::<PySequence>()
        .map_err(|_| PyTypeError::new_err("rows must be a sequence of mappings"))?;
    let row_count = rows.len()?;
    let mut out = Vec::with_capacity(row_count.saturating_mul(512));
    out.push(b'[');
    for idx in 0..row_count {
        if idx > 0 {
            out.push(b',');
        }
        let row = rows.get_item(idx)?;
        let row = row
            .downcast::<PyDict>()
            .map_err(|_| PyTypeError::new_err("rows must contain dict mappings"))?;
        let value = serialize_feed_row_value(row)?;
        serde_json::to_writer(&mut out, &value).map_err(|err| {
            PyValueError::new_err(format!("failed to serialize federation feed row: {err}"))
        })?;
    }
    out.push(b']');
    Ok(PyBytes::new_bound(py, &out).unbind())
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
