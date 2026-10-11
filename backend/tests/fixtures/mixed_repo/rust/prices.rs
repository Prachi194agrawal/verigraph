fn add_tax(amount: f64) -> f64 {
    amount * 1.1
}

fn total(amount: f64) -> f64 {
    add_tax(amount)
}

#[test]
fn test_total() {
    let result = total(10.0);
    assert_eq!(result, 11.0);
}
