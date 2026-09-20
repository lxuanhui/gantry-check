/**
 * OneMap's public address search (no key, CORS open). Called straight from the browser so
 * the Worker never has to proxy it.
 *
 * https://www.onemap.gov.sg/docs/#search
 */

export interface Place {
	name: string;
	address: string;
	lat: number;
	lng: number;
}

interface OneMapResult {
	SEARCHVAL?: string;
	ADDRESS?: string;
	LATITUDE?: string;
	LONGITUDE?: string;
}

const ENDPOINT = 'https://www.onemap.gov.sg/api/common/elastic/search';

/** Top matches for a query. Coordinates come back as strings, and some rows carry none. */
export async function searchPlaces(
	query: string,
	signal?: AbortSignal,
	limit = 6
): Promise<Place[]> {
	const url = `${ENDPOINT}?searchVal=${encodeURIComponent(query)}&returnGeom=Y&getAddrDetails=Y&pageNum=1`;
	const response = await fetch(url, { signal });
	if (!response.ok) throw new Error(`OneMap search failed (HTTP ${response.status}).`);

	const body = (await response.json()) as { found?: number; results?: OneMapResult[] };
	const places: Place[] = [];
	// OneMap happily returns the same building twice (one row per postal code entry), so
	// identical name+coordinate rows are collapsed.
	const seen = new Set<string>();
	for (const result of body.results ?? []) {
		const lat = Number.parseFloat(result.LATITUDE ?? '');
		const lng = Number.parseFloat(result.LONGITUDE ?? '');
		if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
		const name = result.SEARCHVAL ?? result.ADDRESS ?? 'Unnamed place';
		const key = `${name}|${lat.toFixed(6)}|${lng.toFixed(6)}`;
		if (seen.has(key)) continue;
		seen.add(key);
		places.push({ name, address: result.ADDRESS ?? '', lat, lng });
		if (places.length >= limit) break;
	}
	return places;
}
