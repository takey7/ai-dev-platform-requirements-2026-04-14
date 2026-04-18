export function describePlatform(name: string): string {
  return `Platform baseline ready for ${name}.`;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  console.log(describePlatform("{{SERVICE_NAME}}"));
}
