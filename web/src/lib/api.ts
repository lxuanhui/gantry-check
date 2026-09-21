/**
 * Client for the gantry-check API. The UI is served by the same Worker that serves the API,
 * so every path here is relative; in `vite dev` they are proxied to a local API (see
 * `vite.config.ts`).
 */

export type Vehicle = 'car' | 'motorcycle' | 'hgv' | 'vhgv';

/** Mirrors `VehicleType` in src/gantry_check/domain/models.py. */
export const VEHICLES: { value: Vehicle; label: string }[] = [
	{ value: 'car', label: 'Car / taxi / light goods' },
	{ value: 'motorcycle', label: 'Motorcycle' },
	{ value: 'hgv', label: 'Heavy goods / small bus' },
	{ value: 'vhgv', label: 'Very heavy goods / big bus' }
];

export interface Band {
	start: string;
	end: string;
	amount_cents: number;
	amount: string;
}

export interface Charge {
	gantry: string;
	name: string;
	zone_id: string | null;
	crossed_at: string;
	day_type: string;
	band: Band | null;
	amount_cents: number;
	amount: string;
	/** `"line"` (carriageway geometry) or `"point"` (proximity only — direction unverified). */
	method: string;
}

export interface Estimate {
	engine: string;
	summary: string;
	distance_m: number;
	duration_s: number;
	/** Google encoded polyline, precision 5. Absent on older API builds. */
	polyline?: string | null;
	depart_at: string;
	vehicle: Vehicle;
	charges: Charge[];
	total_cents: number;
	total: string;
	warnings: string[];
}

export interface Gantry {
	number: string;
	name: string;
	zone_id: string | null;
	lat: number;
	lng: number;
	has_line?: boolean;
}

export interface EstimateRequest {
	origin: [number, number];
	destination: [number, number];
	depart_at: string;
	vehicle: Vehicle;
}

/** An error the API itself reported, with a status and a human-readable detail. */
export class ApiError extends Error {
	status: number;

	constructor(status: number, message: string) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
	}
}

/**
 * FastAPI reports `detail` as a string for raised HTTPExceptions (502/503) and as a list of
 * pydantic validation errors for a 422.
 */
function detailToMessage(status: number, detail: unknown): string {
	if (typeof detail === 'string' && detail) return detail;
	if (Array.isArray(detail)) {
		const parts = detail
			.map((item) => {
				if (!item || typeof item !== 'object') return null;
				const entry = item as { loc?: unknown[]; msg?: string };
				const field = Array.isArray(entry.loc)
					? entry.loc.filter((p) => p !== 'body').join('.')
					: '';
				return [field, entry.msg].filter(Boolean).join(': ');
			})
			.filter(Boolean);
		if (parts.length) return parts.join('; ');
	}
	return `Request failed (HTTP ${status}).`;
}

async function readError(response: Response): Promise<ApiError> {
	let detail: unknown = null;
	try {
		detail = ((await response.json()) as { detail?: unknown }).detail;
	} catch {
		// Not JSON (a proxy error page, say) — fall through to the generic message.
	}
	return new ApiError(response.status, detailToMessage(response.status, detail));
}

export async function postEstimate(body: EstimateRequest, signal?: AbortSignal): Promise<Estimate> {
	const response = await fetch('/estimate', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
		signal
	});
	if (!response.ok) throw await readError(response);
	return (await response.json()) as Estimate;
}

export async function getGantries(signal?: AbortSignal): Promise<Gantry[]> {
	const response = await fetch('/gantries', { signal });
	if (!response.ok) throw await readError(response);
	return (await response.json()) as Gantry[];
}

/** What to show the user for a failed estimate, by status. */
export function explainError(error: unknown): { title: string; detail: string } {
	if (error instanceof ApiError) {
		if (error.status === 503) {
			return {
				title: 'Routing is unavailable',
				detail: `The server has no routing engine configured, so it cannot work out a route. (${error.message})`
			};
		}
		if (error.status === 502) {
			const said = /[.!?]$/.test(error.message) ? error.message : `${error.message}.`;
			return { title: 'The routing service failed', detail: `${said} Try again in a moment.` };
		}
		if (error.status === 422) {
			return { title: 'Check the trip details', detail: error.message };
		}
		if (error.status === 403) {
			return { title: 'Not available outside Singapore', detail: error.message };
		}
		if (error.status === 429) {
			return { title: 'Slow down a little', detail: error.message };
		}
		return { title: 'The estimate failed', detail: error.message };
	}
	return {
		title: 'Could not reach the server',
		detail: 'Check your connection and try again.'
	};
}
