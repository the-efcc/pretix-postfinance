"use strict";

$(function () {
  document.querySelectorAll(".postfinance-action-group").forEach(function (group) {
    var targetId = group.getAttribute("data-move-after");
    if (!targetId) return;
    var target = document.getElementById(targetId);
    if (!target) return;
    var formGroup = target.closest(".form-group");
    if (!formGroup) return;
    formGroup.parentNode.insertBefore(group, formGroup.nextSibling);
  });

  function getCsrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function findResultEl(btn) {
    var formGroup = btn.closest(".form-group");
    return formGroup ? formGroup.querySelector(".postfinance-action-result") : null;
  }

  function postAction(btn, url, options) {
    var resultEl = findResultEl(btn);
    var originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = options.busyLabel;
    if (resultEl) {
      resultEl.textContent = "";
    }

    var formData = new FormData();
    formData.append("mode", btn.getAttribute("data-mode") || "live");

    fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken() },
      credentials: "same-origin",
      body: formData,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        btn.disabled = false;
        btn.textContent = originalLabel;
        if (resultEl) {
          resultEl.textContent = " " + data.message;
          resultEl.style.color = data.success ? "green" : "red";
        }
      })
      .catch(function (error) {
        btn.disabled = false;
        btn.textContent = originalLabel;
        if (resultEl) {
          resultEl.textContent = " " + options.failureMessage;
          resultEl.style.color = "red";
        }
        console.error("PostFinance " + options.busyLabel + " error:", error);
      });
  }

  document.querySelectorAll(".postfinance-test-connection").forEach(function (btn) {
    btn.addEventListener("click", function () {
      postAction(btn, btn.getAttribute("data-test-url"), {
        busyLabel: gettext("Testing..."),
        failureMessage: gettext("Connection test failed. Please try again."),
      });
    });
  });

  document.querySelectorAll(".postfinance-setup-webhooks").forEach(function (btn) {
    btn.addEventListener("click", function () {
      postAction(btn, btn.getAttribute("data-setup-url"), {
        busyLabel: gettext("Setting up..."),
        failureMessage: gettext("Webhook setup failed. Please try again."),
      });
    });
  });
});
