package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
)

type Billing struct {
	baseURL string
	http    *http.Client
}

// NewBilling builds the billing client; the base comes from BILLING_URL.
func NewBilling(baseURL string) *Billing {
	return &Billing{baseURL: strings.TrimRight(baseURL, "/"), http: http.DefaultClient}
}

// CreateInvoice asks the billing service to open an invoice for a new order.
func (b *Billing) CreateInvoice(ctx context.Context, orderID string, totalCents int) error {
	body, err := json.Marshal(map[string]any{"order_id": orderID, "amount_cents": totalCents})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.baseURL+"/invoices", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := b.http.Do(req)
	if err != nil {
		return err
	}
	return resp.Body.Close()
}
