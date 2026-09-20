/**
 * Singapore-time helpers.
 *
 * The API takes a naive `depart_at` and reads it as Singapore time, and returns timestamps
 * already offset to +08:00. Neither side should ever go through the browser's own timezone,
 * so nothing here uses `Date#getHours()` and friends.
 */

const SG_PARTS = new Intl.DateTimeFormat('en-GB', {
	timeZone: 'Asia/Singapore',
	// hourCycle rather than hour12:false, which can render midnight as "24" in some engines.
	hourCycle: 'h23',
	year: 'numeric',
	month: '2-digit',
	day: '2-digit',
	hour: '2-digit',
	minute: '2-digit'
});

/** Now in Singapore, as a `YYYY-MM-DDTHH:MM` string for `<input type="datetime-local">`. */
export function nowInSingapore(date: Date = new Date()): string {
	const parts = SG_PARTS.formatToParts(date);
	const get = (type: Intl.DateTimeFormatPartTypes) =>
		parts.find((part) => part.type === type)?.value ?? '00';
	return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}

/**
 * `HH:MM` out of an ISO-8601 timestamp, read literally off the string. The API always
 * returns Singapore-offset timestamps, so the wall-clock time is already the right one —
 * parsing to a `Date` would re-render it in the viewer's timezone.
 */
export function clockTime(iso: string): string {
	return iso.slice(11, 16);
}

/** `Fri 22 Sep, 08:00` for a naive or offset ISO-8601 string, without leaving Singapore time. */
export function describeDeparture(iso: string): string {
	const [datePart, timePart = ''] = iso.split('T');
	const [y, m, d] = datePart.split('-').map(Number);
	if (!y || !m || !d) return iso;
	// Midday UTC keeps the date stable whichever way the Date is later interpreted.
	const weekday = new Date(Date.UTC(y, m - 1, d, 12)).toLocaleDateString('en-GB', {
		timeZone: 'UTC',
		weekday: 'short',
		day: 'numeric',
		month: 'short'
	});
	return `${weekday}, ${timePart.slice(0, 5)}`;
}

/** `23 min` / `1 h 05 min` for a duration in seconds. */
export function formatDuration(seconds: number): string {
	const total = Math.round(seconds / 60);
	const hours = Math.floor(total / 60);
	const minutes = total % 60;
	return hours ? `${hours} h ${String(minutes).padStart(2, '0')} min` : `${minutes} min`;
}

/** `14.2 km` (or `850 m` under a kilometre) for a distance in metres. */
export function formatDistance(metres: number): string {
	return metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(1)} km`;
}
