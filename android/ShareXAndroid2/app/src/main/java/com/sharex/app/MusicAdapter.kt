package com.sharex.app

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

class MusicAdapter(
    private var songs: List<MusicFile>,
    private val onSongClick: (MusicFile, Int) -> Unit
) : RecyclerView.Adapter<MusicAdapter.ViewHolder>() {

    private var isGrid = false

    fun setGrid(grid: Boolean) {
        isGrid = grid
        notifyDataSetChanged()
    }

    fun updateList(newSongs: List<MusicFile>) {
        songs = newSongs
        notifyDataSetChanged()
    }

    class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
        val icon: ImageView = view.findViewById(R.id.musicIcon)
        val title: TextView = view.findViewById(R.id.musicTitle)
        val details: TextView = view.findViewById(R.id.musicDetails)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val layout = if (isGrid) R.layout.item_music_grid else R.layout.item_music
        val view = LayoutInflater.from(parent.context).inflate(layout, parent, false)
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        val song = songs[position]
        holder.title.text = song.title
        holder.details.text = if (isGrid) "${song.duration} • ${song.size}" else "${song.artist} • ${song.size} • ${song.duration}"
        holder.itemView.setOnClickListener { onSongClick(song, position) }
    }

    override fun getItemCount() = songs.size
}