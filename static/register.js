document.addEventListener('DOMContentLoaded', () => {
    function showAuthAlert(message, type = 'auto', scope = 'global') {
        const panel = (scope === 'signin' ? document.getElementById('signin-alert') : null) || document.getElementById('auth-global-alert') || document.getElementById('signin-alert');
        if (!panel) return;
        const msg = String(message || 'Please try again.');
        let resolvedType = type;
        if (type === 'auto') {
            resolvedType = /success|sent|verified|created|changed/i.test(msg) ? 'success' : (/session|verify|required|please/i.test(msg) ? 'warning' : 'error');
        }
        panel.className = 'auth-alert auth-alert-' + resolvedType;
        const title = panel.querySelector('.auth-alert-title');
        const body = panel.querySelector('.auth-alert-message');
        if (title) title.textContent = resolvedType === 'success' ? 'Success' : resolvedType === 'warning' ? 'Attention Required' : 'Unable to Continue';
        if (body) body.textContent = msg;
        panel.hidden = false;
        panel.scrollIntoView({behavior:'smooth', block:'nearest'});
    }
    document.querySelectorAll('.auth-alert-close').forEach(btn => btn.addEventListener('click', () => { btn.closest('.auth-alert').hidden = true; }));
    const signInBox = document.getElementById('signin-box');
    const signUpBox = document.getElementById('signup-box');
    const forgotPasswordBox = document.getElementById('forgot-password-box');

    const toSignUpBtn = document.getElementById('to-signup');
    const toForgotPasswordBtn = document.getElementById('to-forgot-password');
    const toSignInLinks = document.querySelectorAll('.to-signin-link, #to-signin');

    const signInForm = document.getElementById('signin-form');
    const signUpForm = document.getElementById('signup-form');
    const forgotPasswordForm = document.getElementById('forgot-password-form');

    let isEmailVerified = false;

    // View Toggles
    function showBox(boxToShow) {
        if (signInBox) signInBox.style.display = 'none';
        if (signUpBox) signUpBox.style.display = 'none';
        if (forgotPasswordBox) forgotPasswordBox.style.display = 'none';
        if (boxToShow) boxToShow.style.display = 'block';
    }

    if (toSignUpBtn) {
        toSignUpBtn.addEventListener('click', (e) => {
            e.preventDefault();
            showBox(signUpBox);
        });
    }

    if (toForgotPasswordBtn) {
        toForgotPasswordBtn.addEventListener('click', (e) => {
            e.preventDefault();
            showBox(forgotPasswordBox);
        });
    }

    toSignInLinks.forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            showBox(signInBox);
        });
    });

    // Registration - Send Code
    const btnSendRegCode = document.getElementById('btn-send-reg-code');
    if (btnSendRegCode) {
        btnSendRegCode.addEventListener('click', async () => {
            const email = document.getElementById('reg-email').value.trim();
            if (!email) {
                showAuthAlert('Please enter an email address first.');
                return;
            }

            btnSendRegCode.disabled = true;
            btnSendRegCode.textContent = 'Sending...';

            try {
                const response = await fetch('/api/send-registration-code', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email })
                });

                const result = await response.json();
                if (response.ok && result.status === 'success') {
                    showAuthAlert('Verification code sent! Please check your email inbox.');
                    document.getElementById('reg-code-wrapper').style.display = 'block';
                } else {
                    showAuthAlert(result.message || 'Failed to send verification code.');
                }
            } catch (err) {
                console.error('Error sending code:', err);
                showAuthAlert('Connection error. Please try again.');
            } finally {
                btnSendRegCode.disabled = false;
                btnSendRegCode.textContent = 'Resend Code';
            }
        });
    }

    // Registration - Verify Code
    const btnVerifyRegCode = document.getElementById('btn-verify-reg-code');
    if (btnVerifyRegCode) {
        btnVerifyRegCode.addEventListener('click', async () => {
            const email = document.getElementById('reg-email').value.trim();
            const code = document.getElementById('reg-code').value.trim();

            if (!code || code.length !== 6) {
                showAuthAlert('Please enter a valid 6-digit code.');
                return;
            }

            btnVerifyRegCode.disabled = true;

            try {
                const response = await fetch('/api/verify-registration-code', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, code })
                });

                const result = await response.json();
                const statusSpan = document.getElementById('reg-code-status');

                if (response.ok && result.status === 'success') {
                    isEmailVerified = true;
                    if (statusSpan) {
                        statusSpan.textContent = 'Email Verified ✓';
                        statusSpan.style.color = '#2D6A4F';
                    }
                    document.getElementById('btn-signup-submit').disabled = false;
                    document.getElementById('reg-email').readOnly = true;
                    btnSendRegCode.style.display = 'none';
                    btnVerifyRegCode.style.display = 'none';
                } else {
                    showAuthAlert(result.message || 'Verification failed.');
                    if (statusSpan) {
                        statusSpan.textContent = 'Invalid or expired code.';
                        statusSpan.style.color = '#d9534f';
                    }
                }
            } catch (err) {
                console.error('Error verifying code:', err);
                showAuthAlert('Connection error. Please try again.');
            } finally {
                btnVerifyRegCode.disabled = false;
            }
        });
    }

    // Handle Sign In Submit
    if (signInForm) {
        signInForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const submitBtn = document.getElementById('btn-signin-submit');
            const username = document.getElementById('login-username').value.trim();
            const password = document.getElementById('login-password').value;

            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = 'Signing in...';
            }

            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });

                const result = await response.json();

                if (result.status === 'success') {
                    if (result.redirect) {
                        window.location.href = result.redirect;
                    } else {
                        window.location.href = '/home';
                    }
                } else {
                    showAuthAlert(result.message || 'The username or password you entered is incorrect.', 'error', 'signin');
                }
            } catch (err) {
                console.error('Sign-in error:', err);
                showAuthAlert('Connection error. Please try again.', 'error', 'signin');
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Sign In';
                }
            }
        });
    }

    // Handle Sign Up Submit
    if (signUpForm) {
        signUpForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            if (!isEmailVerified) {
                showAuthAlert('Please verify your email address before completing registration.');
                return;
            }

            const phone = document.getElementById('reg-phone').value.trim();
            const phoneRegex = /^09\d{9}$/;
            if (!phoneRegex.test(phone)) {
                showAuthAlert('Phone number must contain exactly 11 digits starting with 09 (e.g. 09123456789).');
                return;
            }

            const submitBtn = document.getElementById('btn-signup-submit');
            const firstName = document.getElementById('reg-first').value.trim();
            const middleName = document.getElementById('reg-middle')?.value.trim() || '';
            const lastName = document.getElementById('reg-last').value.trim();
            const suffix = document.getElementById('reg-suffix')?.value.trim() || '';
            const email = document.getElementById('reg-email').value.trim();
            const username = document.getElementById('reg-user').value.trim();
            const block = document.getElementById('reg-block').value.trim();
            const lot = document.getElementById('reg-lot').value.trim();
            const addressLine = document.getElementById('reg-address-line').value.trim();
            const household = document.getElementById('reg-household')?.value || '1';
            const civilStatus = document.getElementById('reg-civil-status')?.value || 'Single';
            const gender = document.getElementById('reg-gender')?.value || 'NA';
            const dob = document.getElementById('reg-dob')?.value || '';
            const emergencyName = document.getElementById('reg-emergency-name')?.value.trim() || '';
            const emergencyNumber = document.getElementById('reg-emergency-number')?.value.trim() || '';
            const emergencyRelationship = document.getElementById('reg-emergency-relationship')?.value.trim() || '';
            if (emergencyNumber && !/^09\d{9}$/.test(emergencyNumber)) {
                showAuthAlert('Emergency contact number must contain 11 digits starting with 09, or leave it blank.');
                return;
            }
            const password = document.getElementById('reg-pass').value;
            const passwordConfirm = document.getElementById('reg-pass-confirm').value;

            if (password !== passwordConfirm) {
                showAuthAlert('Passwords do not match. Please re-enter.');
                return;
            }

            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = 'Creating Account...';
            }

            try {
                const response = await fetch('/api/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        first_name: firstName,
                        middle_name: middleName,
                        last_name: lastName,
                        suffix: suffix,
                        email: email,
                        username: username,
                        phone: phone,
                        block: block,
                        lot: lot,
                        address_line: addressLine,
                        household: household,
                        civil_status: civilStatus,
                        gender: gender,
                        dob: dob,
                        emergency_name: emergencyName,
                        emergency_number: emergencyNumber,
                        emergency_relationship: emergencyRelationship,
                        password: password,
                        password_confirm: passwordConfirm
                    })
                });

                const result = await response.json();

                if (result.status === 'success') {
                    showAuthAlert(result.message);
                    signUpForm.reset();
                    showBox(signInBox);
                } else {
                    showAuthAlert(result.message || 'Registration failed.');
                }
            } catch (err) {
                console.error('Sign-up error:', err);
                showAuthAlert('Connection error. Please check your terminal or server.');
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Sign Up';
                }
            }
        });
    }

    // Handle Forgot Password - Send Code
    const btnSendResetCode = document.getElementById('btn-send-reset-code');
    if (btnSendResetCode) {
        btnSendResetCode.addEventListener('click', async () => {
            const email = document.getElementById('reset-email').value.trim();
            if (!email) {
                showAuthAlert('Please enter your email address.');
                return;
            }

            btnSendResetCode.disabled = true;
            btnSendResetCode.textContent = 'Sending...';

            try {
                const response = await fetch('/api/forgot-password/send-code', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email })
                });

                const result = await response.json();
                if (response.ok && result.status === 'success') {
                    showAuthAlert('Password reset code sent to your email.');
                    document.getElementById('reset-code-group').style.display = 'block';
                    document.getElementById('reset-pass-group').style.display = 'block';
                    document.getElementById('btn-reset-password-submit').style.display = 'block';
                    document.getElementById('reset-email').readOnly = true;
                    btnSendResetCode.style.display = 'none';
                } else {
                    showAuthAlert(result.message || 'Email not found.');
                }
            } catch (err) {
                console.error('Error sending reset code:', err);
                showAuthAlert('Connection error. Please try again.');
            } finally {
                btnSendResetCode.disabled = false;
                btnSendResetCode.textContent = 'Send Code';
            }
        });
    }

    // Handle Forgot Password - Submit Reset
    if (forgotPasswordForm) {
        forgotPasswordForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const email = document.getElementById('reset-email').value.trim();
            const code = document.getElementById('reset-code').value.trim();
            const newPassword = document.getElementById('reset-new-pass').value;
            const submitBtn = document.getElementById('btn-reset-password-submit');

            if (!code || code.length !== 6) {
                showAuthAlert('Please enter the 6-digit reset code.');
                return;
            }

            if (!newPassword || newPassword.length < 8) {
                showAuthAlert('Password must be at least 8 characters.');
                return;
            }

            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = 'Resetting...';
            }

            try {
                const response = await fetch('/api/forgot-password/reset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, code, new_password: newPassword })
                });

                const result = await response.json();

                if (response.ok && result.status === 'success') {
                    showAuthAlert(result.message);
                    forgotPasswordForm.reset();
                    showBox(signInBox);
                } else {
                    showAuthAlert(result.message || 'Reset failed.');
                }
            } catch (err) {
                console.error('Password reset error:', err);
                showAuthAlert('Connection error. Please try again.');
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Reset Password';
                }
            }
        });
    }
});

// Password controls: icon-only eye toggle and icon-only Caps Lock indicator.
(function initPasswordUsability(){
  const EYE_OPEN = '<svg class="eye-icon eye-open" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.7"/></svg>';
  const EYE_CLOSED = '<svg class="eye-icon eye-closed" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 6.2A9.7 9.7 0 0 1 12 6c6 0 9.5 6 9.5 6a16 16 0 0 1-3.1 3.8M6.2 6.2C3.8 8 2.5 12 2.5 12s3.5 6 9.5 6a9.8 9.8 0 0 0 3.2-.5"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
  function setup(){
    document.querySelectorAll('[data-toggle-password]').forEach(function(btn){
      if(btn.dataset.passwordReady) return;
      btn.dataset.passwordReady='1';
      btn.innerHTML=EYE_OPEN;
      btn.addEventListener('click', function(){
        var input=document.getElementById(btn.dataset.togglePassword);
        if(!input) return;
        var reveal=input.type==='password';
        input.type=reveal?'text':'password';
        btn.innerHTML=reveal?EYE_CLOSED:EYE_OPEN;
        btn.setAttribute('aria-label', reveal?'Hide password':'Show password');
        btn.title=reveal?'Hide password':'Show password';
        input.focus({preventScroll:true});
      });
    });
    document.querySelectorAll('.password-field input').forEach(function(input){
      if(input.dataset.capsReady) return;
      input.dataset.capsReady='1';
      var field=input.closest('.password-field');
      if(!field) return;
      var icon=document.createElement('span');
      icon.className='caps-lock-icon';
      icon.setAttribute('aria-label','Caps Lock is on');
      icon.title='Caps Lock is on';
      icon.textContent='⇪';
      icon.hidden=true;
      field.appendChild(icon);
      function caps(e){ if(e.getModifierState) icon.hidden=!e.getModifierState('CapsLock'); }
      input.addEventListener('keydown',caps);
      input.addEventListener('keyup',caps);
      input.addEventListener('focus',function(e){ if(e.getModifierState) caps(e); });
      input.addEventListener('blur',function(){ icon.hidden=true; });
    });
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',setup); else setup();
})();


document.addEventListener('DOMContentLoaded', function(){
  var p=document.getElementById('reg-pass'), c=document.getElementById('reg-pass-confirm'), m=document.getElementById('reg-password-match');
  function check(){ if(!p||!c||!m)return; if(!c.value){m.textContent='';return;} var ok=p.value===c.value; m.textContent=ok?'Passwords match.':'Passwords do not match.'; m.className='password-match-message '+(ok?'is-valid':'is-invalid'); }
  if(p&&c){p.addEventListener('input',check);c.addEventListener('input',check);}
});

// Direct landing-page link to account creation without changing auth behavior.
document.addEventListener('DOMContentLoaded', () => {
    if (window.location.hash === '#signup') {
        const link = document.getElementById('to-signup');
        if (link) link.click();
    }
});
