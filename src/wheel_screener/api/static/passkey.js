// Passkey sign-up and sign-in: fetch options from the server, hand them to the browser's passkey
// prompt, post the result back. The server does every check that matters (challenge, origin,
// signature, user verification); this file only translates between JSON and the byte buffers the
// WebAuthn API speaks, and turns failures into sentences.
//
// Wired by data attributes, so the templates hold no script:
//   <button data-passkey="register" data-invite="TOKEN">   — the invite page
//   <button data-passkey="login" data-next="/portfolio">   — the sign-in page
// and messages go to the element with id="passkey-status".
(function () {
  "use strict";

  function toBytes(b64url) {
    const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(b64 + "===".slice((b64.length + 3) % 4));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function toB64url(buffer) {
    if (!buffer) return null;
    const bytes = new Uint8Array(buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function withBytes(list) {
    return (list || []).map((c) => Object.assign({}, c, { id: toBytes(c.id) }));
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* an empty or non-JSON body */ }
    if (!res.ok) {
      const detail = res.status === 429
        ? "Too many attempts — please wait a minute and try again."
        : (data.error || "Something went wrong. Please try again.");
      throw new Error(detail);
    }
    return data;
  }

  // The browser reports a cancelled prompt, a timeout and "no passkey here" all as
  // NotAllowedError, and deliberately won't say which — so neither can we.
  function explain(err) {
    if (err && err.name === "NotAllowedError") {
      return "The passkey prompt was cancelled or timed out. Please try again.";
    }
    if (err && err.name === "InvalidStateError") {
      return "This device already has a passkey for this account — sign in with it instead.";
    }
    return (err && err.message) || "Something went wrong. Please try again.";
  }

  async function register(inviteToken) {
    const options = await post("/auth/register/options", { invite: inviteToken });
    options.challenge = toBytes(options.challenge);
    options.user = Object.assign({}, options.user, { id: toBytes(options.user.id) });
    options.excludeCredentials = withBytes(options.excludeCredentials);
    const cred = await navigator.credentials.create({ publicKey: options });
    const transports = cred.response.getTransports ? cred.response.getTransports() : [];
    return post("/auth/register/verify", {
      credential: {
        id: cred.id,
        rawId: toB64url(cred.rawId),
        type: cred.type,
        response: {
          clientDataJSON: toB64url(cred.response.clientDataJSON),
          attestationObject: toB64url(cred.response.attestationObject),
          transports: transports,
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    });
  }

  async function login(next) {
    const options = await post("/auth/login/options");
    options.challenge = toBytes(options.challenge);
    options.allowCredentials = withBytes(options.allowCredentials);
    const cred = await navigator.credentials.get({ publicKey: options });
    return post("/auth/login/verify", {
      next: next,
      credential: {
        id: cred.id,
        rawId: toB64url(cred.rawId),
        type: cred.type,
        response: {
          clientDataJSON: toB64url(cred.response.clientDataJSON),
          authenticatorData: toB64url(cred.response.authenticatorData),
          signature: toB64url(cred.response.signature),
          userHandle: toB64url(cred.response.userHandle),
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    });
  }

  function wire(button) {
    const status = document.getElementById("passkey-status");
    const say = (text, isError) => {
      if (!status) return;
      status.textContent = text;
      status.classList.toggle("passkey-error", Boolean(isError));
    };
    if (!window.PublicKeyCredential || !navigator.credentials) {
      button.disabled = true;
      say("This browser can't use passkeys. Try a current Safari, Chrome, Firefox or Edge.", true);
      return;
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      say("Waiting for your passkey…", false);
      try {
        const kind = button.dataset.passkey;
        const done = kind === "register"
          ? await register(button.dataset.invite)
          : await login(button.dataset.next || "/portfolio");
        say("Signed in.", false);
        window.location.assign(done.redirect || "/portfolio");
      } catch (err) {
        say(explain(err), true);
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
  }

  document.querySelectorAll("[data-passkey]").forEach(wire);
})();
