// 提交按钮防抖：报名请求发出后立即禁用按钮，避免用户在等待响应期间重复点击。
// 服务端还有 request_token 幂等兜底，这里只是减少无意义的重复请求。
document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
        return;
    }
    if (!form.querySelector('input[name="request_token"]')) {
        return;
    }
    var button = form.querySelector('button[type="submit"]');
    if (!button || button.disabled) {
        if (button) {
            event.preventDefault();
        }
        return;
    }
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = "提交中…";

    // 若浏览器回退到本页（例如按下后退键），恢复按钮以免永久禁用。
    window.addEventListener("pageshow", function () {
        button.disabled = false;
        if (button.dataset.originalText) {
            button.textContent = button.dataset.originalText;
        }
    }, { once: true });
});
