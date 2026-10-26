// Charlotte mod, step 1: a test that proves mods load on this game.
// About 15 seconds after a save loads, a short note appears on screen.

@wrapMethod(CR4Player) function OnSpawned( spawnData : SEntitySpawnData )
{
	var result : bool;

	result = wrappedMethod( spawnData );
	AddTimer( 'CharlotteHello', 15.0, false );
	return result;
}

@addMethod(CR4Player) timer function CharlotteHello( dt : float, id : int )
{
	theGame.GetGuiManager().ShowNotification( "Charlotte: Hello, Geralt. The mod is working." );
}

// The same note on demand, from the debug console: charlotte_hello()
exec function charlotte_hello()
{
	theGame.GetGuiManager().ShowNotification( "Charlotte: Hello, Geralt. The mod is working." );
}
