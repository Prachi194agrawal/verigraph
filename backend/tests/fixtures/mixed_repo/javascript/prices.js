export function addTax(amount) {
  return amount * 1.1;
}

export function total(amount) {
  return addTax(amount);
}
