"""Explicit recovery of an identified empty composer after a pre-send failure."""
from .core import normalize
from .windows import AutomationError, is_gentoo_profile, recipient_identity


def discard_empty_draft(driver, *, hwnd, pid, recipient=None, recipients=None):
    """Close only the exact, empty, single composer supplied by the caller."""
    if type(hwnd) is not int or hwnd <= 0 or type(pid) is not int or pid <= 0:
        raise ValueError('닫을 빈 작성창의 식별자가 필요합니다.')
    if recipients is None:
        if (not isinstance(recipient, str) or not normalize(recipient)
                or any(c in recipient for c in ('\n', '\r', '\x00'))):
            raise ValueError('빈 작성창의 현재 수신자를 정확히 지정하세요.')
        expected = normalize(recipient)
    else:
        # Internal retry supplies individual, already-verified chips. The public
        # endpoint still accepts only its original single-recipient argument.
        if (recipient is not None or not isinstance(recipients, list) or not recipients
                or any(not isinstance(item, str) or not normalize(item)
                       or any(c in item for c in '\r\n\x00\v\f\x85\u2028\u2029')
                       for item in recipients)):
            raise ValueError('빈 작성창의 현재 수신자 목록을 정확히 지정하세요.')
        identities = [recipient_identity(item) for item in recipients]
        if (any(item[0] != 'gentoo_account' for item in identities)
                or len(set(identities)) != len(identities)):
            raise ValueError('빈 작성창의 수신자 계정은 중복 없이 확인되어야 합니다.')
        expected = '\n'.join(normalize(item) for item in recipients)
    if not is_gentoo_profile(driver.profile) or driver.roles.get('recipient_read', {}).get('backend') != 'win32':
        raise AutomationError('확인된 쿨메신저의 네이티브 작성창만 정리할 수 있습니다.')
    driver.require_input()

    def validate():
        driver.checkpoint()
        roots = driver.roots(driver.roles['body'])
        if len(roots) != 1:
            raise AutomationError('빈 작성창이 하나인 경우에만 정리할 수 있습니다.')
        root = roots[0]
        if int(root.handle) != hwnd or root.process_id() != pid:
            raise AutomationError('작성창이 변경되어 닫지 않았습니다.')
        if (driver.text('recipient_read') != expected
                or driver.text('title') not in ('', '제목을 입력하세요.')
                or driver.text('body') or driver.text('cc_read')):
            raise AutomationError('작성창의 수신자가 다르거나 입력 내용이 있어 닫지 않았습니다.')
        driver.verify_attachments([])
        # Use native WM_CLOSE; UIA close() may fall back to a global Escape key.
        native = driver.role('recipient_read')
        if int(native.handle) != hwnd or native.process_id() != pid:
            raise AutomationError('네이티브 작성창이 변경되어 닫지 않았습니다.')
        return native

    validate()
    root = validate()
    root.close()
    # A remaining window or confirmation dialog is never clicked automatically.
    if any(int(item.handle) == hwnd for item in driver.roots(driver.roles['body'])):
        raise AutomationError('빈 작성창이 아직 열려 있습니다. 닫기 요청을 반복하지 않습니다.')
    return {'ok': True, 'closed_hwnd': hwnd}
