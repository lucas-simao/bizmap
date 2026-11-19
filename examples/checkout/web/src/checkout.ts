import { api } from './api/client';

export interface Cart {
  userId: string;
  totalCents: number;
}

// placeOrder submits the cart when the shopper clicks "Pay".
export async function placeOrder(cart: Cart): Promise<string> {
  const { data } = await api.post('/orders', {
    user_id: cart.userId,
    total_cents: cart.totalCents,
  });
  return data.id;
}

// pollOrder refreshes the confirmation page until billing marks the order paid.
export async function pollOrder(id: string): Promise<string> {
  const { data } = await api.get(`/orders/${id}`);
  return data.status;
}
