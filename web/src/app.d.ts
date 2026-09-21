/// <reference types="google.maps" />
// The Maps JavaScript API is loaded at runtime, so its `google.maps` namespace only exists
// as an ambient type; the reference above pulls it in for `svelte-check`.

// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface PageState {}
		// interface Platform {}
	}
}

export {};
