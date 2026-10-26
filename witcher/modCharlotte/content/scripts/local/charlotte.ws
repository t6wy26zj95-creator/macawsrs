// Charlotte: a companion who finds Geralt at calm moments, talks with him by voice and walks with him.
//
// The talking happens outside the game: a helper program on the PC listens to the mic, asks the bot what
// she says and plays her voice. It reaches the game through the script debug channel (the game started
// with -net -debugscripts) and calls the chl_* exec functions below. Each one answers with Log() lines
// tagged "[MCW#<seq>]" and a final "[MCW#<seq>] end", the format the helper's network client expects.

// ---------------- state, kept on the player ----------------

@addField(CR4Player) var chlNpc : CNewNPC;
@addField(CR4Player) var chlFollowId : int;
@addField(CR4Player) var chlLeaving : bool;
@addField(CR4Player) var chlLeftAt : float;
@addField(CR4Player) var chlSpawnedAt : float;
@addField(CR4Player) var chlCalmSince : float;

// thePlayer can't be assigned through from a global function, so the fields change through these.
@addMethod(CR4Player) function CHL_SetNpc( npc : CNewNPC ) { chlNpc = npc; }
@addMethod(CR4Player) function CHL_SetFollow( id : int ) { chlFollowId = id; }
@addMethod(CR4Player) function CHL_SetLeaving( leaving : bool, at : float ) { chlLeaving = leaving; chlLeftAt = at; }
@addMethod(CR4Player) function CHL_SetSpawnedAt( at : float ) { chlSpawnedAt = at; }

@wrapMethod(CR4Player) function OnSpawned( spawnData : SEntitySpawnData )
{
	var result : bool;

	result = wrappedMethod( spawnData );
	AddTag( 'chl_geralt' );
	AddTimer( 'CHL_Tick', 1.0, true );
	return result;
}

// ---------------- helpers ----------------

function CHL_Line( seq : int, text : string )
{
	Log( "[MCW#" + IntToString( seq ) + "] " + text );
}

function CHL_End( seq : int )
{
	Log( "[MCW#" + IntToString( seq ) + "] end" );
}

function CHL_B( b : bool ) : string
{
	if ( b )
		return "1";
	return "0";
}

// She is in the world and not on her way out.
function CHL_Here() : bool
{
	if ( thePlayer.chlNpc )
		return !thePlayer.chlLeaving;
	return false;
}

function CHL_Now() : float
{
	return theGame.GetEngineTimeAsSeconds();
}

// Something that must never be interrupted: a fight, enemies about, a conversation or a cutscene.
function CHL_Danger() : bool
{
	return thePlayer.IsInCombat() || thePlayer.IsThreatened() || theGame.IsDialogOrCutscenePlaying()
		|| thePlayer.IsInNonGameplayCutscene() || theGame.IsCurrentlyPlayingNonGameplayScene();
}

// Not dangerous, but not a moment for a visit either.
function CHL_Busy() : string
{
	if ( theGame.GetGuiManager().IsAnyMenu() ) return "menu";
	if ( theGame.IsPaused() ) return "paused";
	if ( theGame.IsBlackscreen() || theGame.IsFading() ) return "fading";
	if ( thePlayer.IsSwimming() || thePlayer.IsDiving() ) return "water";
	if ( thePlayer.IsInAir() ) return "air";
	if ( thePlayer.IsUsingVehicle() ) return "riding";
	if ( thePlayer.IsInFistFightMiniGame() ) return "fistfight";
	if ( thePlayer.IsInInterior() ) return "inside";
	return "";
}

function CHL_Weather() : string
{
	switch ( GetCurWeather() )
	{
		case EWE_Clear: return "clear";
		case EWE_Rain: return "rain";
		case EWE_Snow: return "snow";
		case EWE_Storm: return "storm";
	}
	return "cloudy";
}

// A spot `dist` metres behind the camera on the ground, at about Geralt's height.
function CHL_Behind( dist : float ) : Vector
{
	var dir, pos : Vector;

	dir = theCamera.GetCameraDirection();
	dir.Z = 0;
	dir = VecNormalize( dir );
	pos = thePlayer.GetWorldPosition() - dir * dist;
	return CHL_Ground( pos );
}

function CHL_Ground( pos : Vector ) : Vector
{
	var hit, normal : Vector;

	if ( theGame.GetWorld().StaticTrace( pos + Vector( 0, 0, 5 ), pos - Vector( 0, 0, 5 ), hit, normal ) )
		return hit;
	return pos;
}

function CHL_StopFollowing()
{
	if ( thePlayer.chlNpc && thePlayer.chlFollowId != 0 )
	{
		thePlayer.chlNpc.CancelAIBehavior( thePlayer.chlFollowId );
	}
	thePlayer.CHL_SetFollow( 0 );
}

function CHL_Remove()
{
	CHL_StopFollowing();
	if ( thePlayer.chlNpc )
	{
		thePlayer.chlNpc.Destroy();
	}
	thePlayer.CHL_SetNpc( NULL );
	thePlayer.CHL_SetLeaving( false, 0 );
}

// ---------------- the once-a-second check ----------------

@addMethod(CR4Player) timer function CHL_Tick( dt : float, id : int )
{
	var dist : float;

	if ( CHL_Danger() || CHL_Busy() != "" )
		chlCalmSince = 0;
	else if ( chlCalmSince == 0 )
		chlCalmSince = CHL_Now();

	if ( !chlNpc )
		return;
	dist = VecDistance( chlNpc.GetWorldPosition(), GetWorldPosition() );
	if ( chlLeaving )
	{
		// Out of sight, or walking away for long enough: gone.
		if ( dist > 35 || CHL_Now() - chlLeftAt > 30 )
			CHL_Remove();
		return;
	}
	if ( dist > 60 && chlFollowId != 0 )
	{
		// Left far behind (fast travel, a gallop): she catches up out of view.
		chlNpc.Teleport( CHL_Behind( 8.0 ) );
	}
}

// ---------------- what the helper calls ----------------

// The state of things, every second or so. full=1 adds the quest lines.
exec function chl_poll( seq : int, optional full : int )
{
	var calmFor : int;
	var near : int;
	var busy : string;
	var journal : CWitcherJournalManager;
	var tracked : CJournalQuest;
	var goal : CJournalQuestObjective;

	// Main menu or a loading screen: no player to ask about yet.
	if ( !thePlayer || !GetWitcherPlayer() || !theGame.GetCommonMapManager() || !theGame.GetGuiManager() )
	{
		CHL_Line( seq, "st ingame 0 calmfor 0 danger 1 her 0 follow 0" );
		CHL_End( seq );
		return;
	}
	busy = CHL_Busy();
	calmFor = 0;
	if ( thePlayer.chlCalmSince > 0 )
		calmFor = FloorF( CHL_Now() - thePlayer.chlCalmSince );
	near = -1;
	if ( thePlayer.chlNpc )
		near = FloorF( VecDistance( thePlayer.chlNpc.GetWorldPosition(), thePlayer.GetWorldPosition() ) * 10 );

	CHL_Line( seq, "st ingame 1 calmfor " + IntToString( calmFor ) + " danger " + CHL_B( CHL_Danger() )
		+ " settle " + CHL_B( thePlayer.IsInSettlement() ) + " level " + IntToString( GetWitcherPlayer().GetLevel() )
		+ " coin " + IntToString( thePlayer.GetInventory().GetMoney() )
		+ " hour " + IntToString( GameTimeHours( theGame.GetGameTime() ) )
		+ " her " + CHL_B( CHL_Here() ) + " near10 " + IntToString( near )
		+ " follow " + CHL_B( thePlayer.chlFollowId != 0 ) );
	CHL_Line( seq, "busy " + busy );
	if ( thePlayer.chlNpc )
	{
		// Where she is from the camera's point of view, so the helper can place her voice.
		CHL_Line( seq, "ear " + CHL_Pos10( thePlayer.chlNpc.GetWorldPosition() + Vector( 0, 0, 1.6 ) )
			+ " " + CHL_Pos10( thePlayer.GetWorldPosition() + Vector( 0, 0, 1.6 ) ) + " " + CHL_Pos10( theCamera.GetCameraDirection() ) );
	}
	CHL_Line( seq, "area " + NameToString( theGame.GetCommonMapManager().GetCurrentJournalAreaByPosition( thePlayer.GetWorldPosition() ) ) );
	CHL_Line( seq, "weather " + CHL_Weather() );
	if ( full == 1 )
	{
		journal = theGame.GetJournalManager();
		tracked = journal.GetTrackedQuest();
		if ( tracked )
			CHL_Line( seq, "quest " + GetLocStringById( tracked.GetTitleStringId() ) );
		goal = journal.GetHighlightedObjective();
		if ( goal )
			CHL_Line( seq, "goal " + GetLocStringById( goal.GetTitleStringId() ) );
	}
	CHL_End( seq );
}

// Story quests already finished (so she doesn't spoil the rest), a page at a time.
exec function chl_story( seq : int, offset : int )
{
	var journal : CWitcherJournalManager;
	var entries : array<CJournalBase>;
	var tracked : CJournalQuest;
	var i, found, shown : int;

	journal = theGame.GetJournalManager();
	journal.GetActivatedOfType( 'CJournalQuest', entries );
	found = 0;
	shown = 0;
	for ( i = 0; i < entries.Size(); i += 1 )
	{
		tracked = (CJournalQuest)entries[i];
		if ( !tracked || journal.GetEntryStatus( tracked ) != JS_Success )
			continue;
		// story quests only (the journal's main and chapter types), like the game's own quest list does
		if ( tracked.GetType() != 0 && tracked.GetType() != 1 && tracked.GetType() != 6 && tracked.GetType() != 7 )
			continue;
		if ( found >= offset && shown < 12 )
		{
			CHL_Line( seq, "done " + GetLocStringById( tracked.GetTitleStringId() ) );
			shown += 1;
		}
		found += 1;
	}
	CHL_Line( seq, "total " + IntToString( found ) );
	CHL_End( seq );
}

// She appears out of sight behind Geralt and walks up to him. template = a resource alias, e.g. "sorceress".
exec function chl_spawn( seq : int, template : string )
{
	var res : CEntityTemplate;
	var pos : Vector;
	var rot : EulerAngles;
	var ent : CEntity;

	if ( thePlayer.chlNpc )
		CHL_Remove();
	if ( StrFindFirst( template, ".w2ent" ) >= 0 )
		res = (CEntityTemplate)LoadResource( template, true );  // a full path to a .w2ent
	else
		res = (CEntityTemplate)LoadResource( template );  // a short name from the game's list
	if ( !res )
	{
		CHL_Line( seq, "error no-template" );
		CHL_End( seq );
		return;
	}
	pos = CHL_Behind( 18.0 );
	rot = thePlayer.GetWorldRotation();
	ent = theGame.CreateEntity( res, pos, rot );
	thePlayer.CHL_SetNpc( (CNewNPC)ent );
	if ( !thePlayer.chlNpc )
	{
		if ( ent )
			ent.Destroy();
		CHL_Line( seq, "error not-an-npc" );
		CHL_End( seq );
		return;
	}
	thePlayer.chlNpc.AddTag( 'chl_charlotte' );
	thePlayer.chlNpc.SetTemporaryAttitudeGroup( 'friendly_to_player', AGP_Default );
	thePlayer.chlNpc.SetImmortalityMode( AIM_Invulnerable, AIC_Default );
	thePlayer.CHL_SetLeaving( false, 0 );
	thePlayer.CHL_SetFollow( 0 );
	thePlayer.CHL_SetSpawnedAt( CHL_Now() );
	thePlayer.chlNpc.ActionMoveToDynamicNodeAsync( thePlayer, MT_Walk, 1.0, 2.2 );
	CHL_Line( seq, "spawned 1" );
	CHL_End( seq );
}

// Once she is close: stand and face him, eyes on him.
exec function chl_face( seq : int )
{
	if ( thePlayer.chlNpc )
	{
		thePlayer.chlNpc.ActionRotateToAsync( thePlayer.GetWorldPosition() );
		thePlayer.chlNpc.EnableDynamicLookAt( thePlayer, 600 );
	}
	CHL_End( seq );
}

// If she got stuck on the way: put her a few steps behind him.
exec function chl_closer( seq : int )
{
	if ( thePlayer.chlNpc )
	{
		thePlayer.chlNpc.Teleport( CHL_Behind( 5.0 ) );
		thePlayer.chlNpc.ActionMoveToDynamicNodeAsync( thePlayer, MT_Walk, 1.0, 2.2 );
	}
	CHL_End( seq );
}

// What she does next: stay | follow | wait | leave | vanish (gone at once, for cutscenes and fights).
exec function chl_action( seq : int, action : string )
{
	var ai : CAIFollowSideBySideAction;

	if ( !thePlayer.chlNpc )
	{
		CHL_Line( seq, "error no-charlotte" );
		CHL_End( seq );
		return;
	}
	if ( action == "follow" )
	{
		CHL_StopFollowing();
		ai = new CAIFollowSideBySideAction in thePlayer.chlNpc;
		ai.OnCreated();
		ai.params.targetTag = 'chl_geralt';
		thePlayer.CHL_SetFollow( thePlayer.chlNpc.ForceAIBehavior( ai, BTAP_Emergency ) );
	}
	else if ( action == "wait" || action == "stay" )
	{
		CHL_StopFollowing();
		thePlayer.chlNpc.ActionCancelAll();
		thePlayer.chlNpc.ActionRotateToAsync( thePlayer.GetWorldPosition() );
	}
	else if ( action == "leave" )
	{
		CHL_StopFollowing();
		thePlayer.chlNpc.DisableLookAt();
		thePlayer.chlNpc.ActionMoveAwayFromNodeAsync( thePlayer, 40, MT_Walk );
		thePlayer.CHL_SetLeaving( true, CHL_Now() );
	}
	else if ( action == "vanish" )
	{
		CHL_Remove();
	}
	CHL_Line( seq, "ok " + action );
	CHL_End( seq );
}

// A short note in the corner (mic on or off, the helper's own messages).
exec function chl_note( seq : int, text : string )
{
	theGame.GetGuiManager().ShowNotification( text );
	CHL_End( seq );
}

// x,y,z in tenths of a metre (or of a unit, for a direction).
function CHL_Pos10( v : Vector ) : string
{
	return IntToString( RoundF( v.X * 10 ) ) + " " + IntToString( RoundF( v.Y * 10 ) ) + " " + IntToString( RoundF( v.Z * 10 ) );
}

function CHL_Pos( v : Vector ) : string
{
	return IntToString( FloorF( v.X ) ) + "," + IntToString( FloorF( v.Y ) ) + "," + IntToString( FloorF( v.Z ) );
}

// Where she is and whether she can be seen, for the log.
exec function chl_where( seq : int )
{
	var her, me : Vector;

	if ( thePlayer.chlNpc )
	{
		her = thePlayer.chlNpc.GetWorldPosition();
		me = thePlayer.GetWorldPosition();
		CHL_Line( seq, "where her " + CHL_Pos( her ) + " me " + CHL_Pos( me )
			+ " look " + NameToString( thePlayer.chlNpc.GetAppearance() )
			+ " visible " + CHL_B( thePlayer.chlNpc.GetVisibility() ) );
	}
	else
		CHL_Line( seq, "where none" );
	CHL_End( seq );
}

exec function chl_ping( seq : int )
{
	CHL_Line( seq, "pong charlotte 1" );
	CHL_End( seq );
}
