package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"os"
)

type Billing struct{}

func NewBilling() *Billing {
	return &Billing{}
}

// CreateInvoice asks the billing service to open an invoice for a new order.
func (b *Billing) CreateInvoice(ctx context.Context, orderID string, totalCents int) error {
	body, err := json.Marshal(map[string]any{"order_id": orderID, "amount_cents": totalCents})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, os.Getenv("BILLING_URL")+"/invoices", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	return resp.Body.Close()
}
