use calculator::{divide, multiply};

#[test]
fn test_multiply() {
    assert_eq!(multiply(3.0, 4.0), 12.0);
}

#[test]
fn test_divide_normal() {
    assert_eq!(divide(10.0, 2.0).unwrap(), 5.0);
}

#[test]
fn test_divide_by_zero() {
    assert!(divide(10.0, 0.0).is_err(), "Expected error on division by zero");
}
