@tool
class_name Player
extends CharacterBody2D

signal health_changed(old_value, new_value)
signal died

enum State {
	IDLE,
	RUNNING,
	DEAD,
}

const BulletScene = preload("res://bullet.tscn")
const HELPER = preload("helper.gd")

@export var speed: float = 200.0
@onready var sprite: Sprite2D = $Sprite2D

var health: int = 100
var pickup_quantity: int = 0


class Inventory:
	var items: Array = []

	func add_item(item) -> void:
		items.append(item)


func _ready() -> void:
	died.connect(_on_died)
	set_process(true)


func take_damage(amount: int) -> void:
	var old := health
	health -= amount
	health_changed.emit(old, health)
	if health <= 0:
		emit_signal("died")


func heal(amount: int) -> void:
	take_damage(-amount)


func fire() -> void:
	var bullet = BulletScene.instantiate()
	bullet.launch()


func _on_died() -> void:
	queue_free()
