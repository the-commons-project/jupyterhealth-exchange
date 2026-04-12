export function parseInviteCode(link: string) {
  const parts = link.split('~');
  if (parts.length < 4) throw new Error('Invalid invite code format');
  return {
    host: parts[0],
    clientId: parts[1],
    authCode: parts[2],
    codeVerifier: parts[3],
  };
}
