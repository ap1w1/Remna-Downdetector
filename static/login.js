const passwordInput = document.querySelector('#password');
const passwordToggle = document.querySelector('#password-toggle');

if (passwordInput && passwordToggle) {
  passwordToggle.addEventListener('click', () => {
    const visible = passwordInput.type === 'password';
    passwordInput.type = visible ? 'text' : 'password';
    passwordToggle.classList.toggle('visible', visible);
    passwordToggle.setAttribute('aria-pressed', String(visible));
    passwordToggle.setAttribute('aria-label', visible ? 'Скрыть пароль' : 'Показать пароль');
    passwordInput.focus();
  });
}
