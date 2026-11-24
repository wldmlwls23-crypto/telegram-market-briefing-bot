import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import build from "./railway.ts";

const { resources, variables } = build();
assert.equal(variables.managed, false);
assert.equal(resources.length, 3);
for (const [name, file] of [
  ["telegram-market-briefing-bot", "railway.json"],
  ["market-pulse-cron", "railway.cron.json"],
]) {
  const service = resources.find(resource => resource.name === name);
  const legacy = JSON.parse(readFileSync(new URL("../tests/fixtures/" + file, import.meta.url)));
  for (const section of ["build", "deploy"]) {
    for (const [key, value] of Object.entries(legacy[section])) {
      assert.deepEqual(service[section][key], value, name + "." + section + "." + key);
    }
  }
}
const mounts = resources.flatMap(resource => Object.values(resource.volumeAttachments ?? {}));
assert.equal(mounts.length, 1);
assert.equal(mounts[0].mountPath, "/data");
assert.equal(mounts[0].volume, "volume.telegram-market-briefing-bot-volume");
assert.equal(resources.filter(resource => resource.deploy?.cronSchedule).length, 1);
console.log("Both legacy configurations, the scheduler and the volume match.");
