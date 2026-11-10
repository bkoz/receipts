const form = document.querySelector("#login-form");
const errorMessage = document.querySelector("#login-error");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorMessage.hidden = true;
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  submit.textContent = "Checking…";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      credentials: "same-origin",
    });
    if (response.ok && response.redirected) {
      window.location.assign("/");
      return;
    }
    if (response.status === 503) {
      errorMessage.textContent = await response.text();
    } else if (response.status === 403) {
      errorMessage.textContent = "Login could not be verified. Reload the page and try again.";
    } else {
      errorMessage.textContent = "That password did not match. Try again.";
    }
    errorMessage.hidden = false;
  } catch {
    errorMessage.textContent = "Could not reach the app. Check your connection and try again.";
    errorMessage.hidden = false;
  } finally {
    submit.disabled = false;
    submit.innerHTML = 'Continue <span aria-hidden="true">→</span>';
  }
});
