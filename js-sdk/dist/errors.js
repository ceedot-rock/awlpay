/**
 * AwLPay error classes. All SDK errors extend AwlpayError so callers can
 * catch a single base class and inspect `code` for the specific failure.
 */
export class AwlpayError extends Error {
    code;
    constructor(message, code) {
        super(message);
        this.name = "AwlpayError";
        this.code = code;
        Object.setPrototypeOf(this, new.target.prototype);
    }
}
/** Thrown when a payment exceeds the per-call cap or the wallet's lifetime spend cap. */
export class SpendCapExceededError extends AwlpayError {
    constructor(message) {
        super(message, "SPEND_CAP_EXCEEDED");
        this.name = "SpendCapExceededError";
    }
}
/**
 * Thrown when no rail has enough balance to cover a payment.
 * Carries the per-rail balances (in USD) so the caller can show the user
 * where funds need topping up.
 */
export class InsufficientFundsError extends AwlpayError {
    balances;
    constructor(message, balances) {
        super(message, "INSUFFICIENT_FUNDS");
        this.name = "InsufficientFundsError";
        this.balances = balances;
    }
}
/** Thrown when mainnet is requested without the exact confirmation string. */
export class MainnetConfirmationError extends AwlpayError {
    constructor(message) {
        super(message, "MAINNET_CONFIRMATION");
        this.name = "MainnetConfirmationError";
    }
}
/** Thrown when a rail id is not one the SDK supports. */
export class UnsupportedRailError extends AwlpayError {
    constructor(message) {
        super(message, "UNSUPPORTED_RAIL");
        this.name = "UnsupportedRailError";
    }
}
//# sourceMappingURL=errors.js.map