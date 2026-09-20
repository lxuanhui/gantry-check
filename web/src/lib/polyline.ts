/**
 * Decoder for Google's encoded polyline format (the `polyline` field of `POST /estimate`,
 * emitted by `gantry_check.domain.geo.encode_polyline` at precision 5, latitude first).
 *
 * Small enough to keep in-tree rather than take a dependency for it.
 */
export function decodePolyline(encoded: string, precision = 5): [number, number][] {
	const factor = 10 ** precision;
	const points: [number, number][] = [];
	let index = 0;
	let lat = 0;
	let lng = 0;

	while (index < encoded.length) {
		let shift = 0;
		let result = 0;
		let byte: number;

		// Each coordinate is a zig-zag encoded delta, split into 5-bit chunks with the
		// continuation bit (0x20) set on every chunk but the last, offset by 63.
		do {
			byte = encoded.charCodeAt(index++) - 63;
			result |= (byte & 0x1f) << shift;
			shift += 5;
		} while (byte >= 0x20 && index < encoded.length);
		lat += result & 1 ? ~(result >> 1) : result >> 1;

		shift = 0;
		result = 0;
		do {
			byte = encoded.charCodeAt(index++) - 63;
			result |= (byte & 0x1f) << shift;
			shift += 5;
		} while (byte >= 0x20 && index < encoded.length);
		lng += result & 1 ? ~(result >> 1) : result >> 1;

		points.push([lat / factor, lng / factor]);
	}

	return points;
}
