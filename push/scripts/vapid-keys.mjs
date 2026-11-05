// One-off: make the VAPID key pair for bet alerts, and a pairing code and alert token.
// Run with Node 20+ (no install): node push/scripts/vapid-keys.mjs
// Paste each value into the Worker's secrets (push/README.md, step 4); ALERT_TOKEN also
// goes into GitHub. Nothing is saved anywhere: run it again for new values.
import { b64urlEncode } from "../src/webpush.js";

const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
const pub = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey));
const jwk = await crypto.subtle.exportKey("jwk", pair.privateKey);
const words = () => b64urlEncode(crypto.getRandomValues(new Uint8Array(32)));
const pairCode = String(crypto.getRandomValues(new Uint32Array(1))[0] % 1e8).padStart(8, "0");

console.log(`VAPID_PUBLIC_KEY   ${b64urlEncode(pub)}`);
console.log(`VAPID_PRIVATE_KEY  ${jwk.d}`);
console.log(`ALERT_TOKEN        ${words()}`);
console.log(`PAIR_CODE          ${pairCode}`);
