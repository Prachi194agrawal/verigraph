package prices

func AddTax(amount float64) float64 {
    return amount * 1.1
}

func Total(amount float64) float64 {
    return AddTax(amount)
}
