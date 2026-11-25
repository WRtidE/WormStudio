export async function fetchProgress(revision, signal) {
  const headers = revision ? {'X-Progress-Revision': revision} : {};
  const response = await fetch('/api/progress', {cache: 'no-store', headers, signal});
  if (!response.ok) throw Error('进度暂不可用');
  return response.json();
}
