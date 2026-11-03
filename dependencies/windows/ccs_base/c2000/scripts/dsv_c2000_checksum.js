/* Import DebugServer enving and Java packages */
importPackage(Packages.com.ti.debug.engine.scripting);
importPackage(Packages.com.ti.ccstudio.scripting.environment);
importPackage(Packages.java.lang);

var env = ScriptingEnvironment.instance();
var server = env.getServer("DebugServer.1");
server.setConfig("../../c2000/scripts/DSV_C2000.ccxml");
var session = server.openSession();

// Parse start addresses from arguments[1]
var startAddresses = [];
if (arguments[1]) {
    startAddresses = arguments[1].split(','); // Split by commas
} else {
    startAddresses = [0x80000]; // Default value
}

// Parse end addresses from arguments[2]
var endAddresses = [];
if (arguments[2]) {
    endAddresses = arguments[2].split(','); // Split by commas
} else {
    endAddresses = [0xAFFFF]; // Default value
}

// default page and blank memory value
var page = 0x0;
var defaultVal = 0xFFFF;

try{
	// setup; don't run to main, and fill the flash locations in DSV with 0xFFFF
	session.options.setBoolean("AutoRunToLabelOnRestart",false);

	// Fill each memory range from startAddresses to endAddresses
    for (var i = 0; i < startAddresses.length; i++) {
        var startAddress = startAddresses[i];
        var endAddress = endAddresses[i];

        // Fill the flash locations in DSV with 0xFFFF
        session.memory.fill(startAddress, page, endAddress - startAddress + 1, defaultVal);
    }

	// load program
	session.memory.loadProgram(arguments[0]);

	// Calculate flash checksum
	var checksum = 0;

	// Iterate through each memory range to calculate checksum
	for (var i = 0; i < startAddresses.length; i++) {
        var startAddress = startAddresses[i];
        var endAddress = endAddresses[i];
        var arr = session.memory.readData(page, startAddress, 16, endAddress - startAddress + 1);
        
        for (var j = 0; j < arr.length; j++) {
            checksum += arr[j];
        }
    }
	checksum = checksum&0xFFFF;
	
	env.traceWrite("0x"+checksum.toString(16));
}
catch(err)
{
	env.traceWrite("Error." + err);
}

/* End session, since the tests are done */
server.stop();
session.terminate();
