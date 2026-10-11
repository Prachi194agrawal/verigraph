package prices

import "testing"

func TestTotal(t *testing.T) {
    if Total(10) != 11 {
        t.Fatal("unexpected total")
    }
}
