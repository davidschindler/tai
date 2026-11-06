// Runs inside the VS Code extension host: loads the mocha suite.
import * as path from "path";

export function run(): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const Mocha = require("mocha");
  const real = !!process.env.TAI_REAL;
  // the real-API run only executes the "real API" suite; normal runs skip it
  const mocha = new Mocha({ ui: "bdd", timeout: 60000, color: false, reporter: "spec", grep: "real API", invert: !real });
  mocha.addFile(path.join(__dirname, "suite.js"));
  return new Promise((resolve, reject) => {
    mocha.run((failures: number) => (failures ? reject(new Error(`${failures} tests failed`)) : resolve()));
  });
}
