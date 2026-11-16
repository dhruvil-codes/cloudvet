pub fn divide(a: f64, b: f64) -> Result<f64, String> {
    // BUG: Missing zero check, returns raw division resulting in infinity!
    Ok(a / b)
}

pub fn multiply(a: f64, b: f64) -> f64 {
    a * b
}
