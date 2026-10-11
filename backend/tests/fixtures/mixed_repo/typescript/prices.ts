export function addTax(amount: number): number {
  return amount * 1.1;
}

export class Checkout {
  total(amount: number): number {
    return addTax(amount);
  }
}
