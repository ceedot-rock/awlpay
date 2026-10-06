/**
 * AwLPay error classes. All SDK errors extend AwlpayError so callers can
 * catch a single base class and inspect `code` for the specific failure.
 */
export declare class AwlpayError extends Error {
    readonly code: string;
    constructor(message: string, code: string);
}
/** Thrown when a payment exceeds the per-call cap or the wallet's lifetime spend cap. */
export declare class SpendCapExceededError extends AwlpayError {
    constructor(message: string);
}
/**
 * Thrown when no rail has enough balance to cover a payment.
 * Carries the per-rail balances (in USD) so the caller can show the user
 * where funds need topping up.
 */
export declare class InsufficientFundsError extends AwlpayError {
    readonly balances: Record<string, number>;
    constructor(message: string, balances: Record<string, number>);
}
/** Thrown when mainnet is requested without the exact confirmation string. */
export declare class MainnetConfirmationError extends AwlpayError {
    constructor(message: string);
}
/** Thrown when a rail id is not one the SDK supports. */
export declare class UnsupportedRailError extends AwlpayError {
    constructor(message: string);
}
//# sourceMappingURL=errors.d.ts.map