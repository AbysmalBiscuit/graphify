extends Node

var current_weapon: Weapon
var speed_limit: int
var hitbox: Area3D
var extra_ammo: Array[Weapon]


func hit(by: Weapon) -> Weapon:
	current_weapon = by
	return by


func ammo_cap() -> int:
	return Weapon.MAX_AMMO


func use_weapon() -> void:
	current_weapon.reload()
	var x = current_weapon.ammo
