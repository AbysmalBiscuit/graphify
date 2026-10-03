class_name Weapon
extends Resource

const MAX_AMMO = 30

var damage: int = 10


static func build() -> Weapon:
	return Weapon.new()


static func rebuild() -> Weapon:
	return Weapon.build()
