import * as React from 'react'
import socket from '../socket'

const hideSendButton = true

const colors = {
    owner: '#daa520',
    admin: '#ca20d9',
    member: '#208fd9'
}

const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export const TextChat = (props) => {
    const [messages, updateMessages] = React.useState([])

    const seenDates = {}
    const seenTimes = {}

    const formatedTime = (timestamp) => {
        const date = new Date(timestamp)

        let [month, day, year, hour, minutes] = [
            date.getMonth(),
            date.getDate(),
            date.getFullYear(),
            date.getHours(),
            date.getMinutes()
        ]

        let display_date = display_date = `${months[month]} ${day}, ${year}`
        let display_time = ''

        if (minutes < 10) {
            minutes = `0${minutes}`
        }

        if (hour > 12) {
            display_time = `${hour - 12}:${minutes} PM`
        } else {
            if (hour === 0) {
                display_time = `12:${minutes} AM`
            } else {
                display_time = `${hour}:${minutes} AM`
            }
        }

        if (seenDates[`${display_date}`]) {
            display_date = ''
        } else {
            seenDates[`${display_date}`] = true
        }

        if (seenTimes[`${display_time}`]) {
            display_time = ''
        } else {
            seenTimes[`${display_time}`] = true
        }

        return {
            date: `${display_date}`,
            time: `${display_time}`,
        }
    }

    const MessageContent = (props) => {
        const {message, memberDetails, i, total} = props
        const {date, time} = formatedTime(message.timestamp)

        const margin = i === total - 1 ? '50px' : '0px'

        return <>
            {date.length ?
                <div className='message_date' key={date}>{date}</div>
            : <></>}
            <div key={message.timestamp} className='message_content' style={{marginBottom: `${margin}`}}>
                <div className='message_timestamp'>{time}</div>
                <div className='message_text'>
                    <b style={{'color': colors[memberDetails[message.user]?.role ?? 'member']}}>
                        {memberDetails[message.user].name}
                    </b>
                    {message.content}
                </div>
            </div>
        </>
    }

    socket.on("receive-message", (chatID) => {
        // currentChat._id check ? idk yet.
    })

    const handleKeyDown = (event) => {
        if (event.key === 'Enter' && event.target.id === 'Chat_Input' && event.target.value) {
            sendChat({
                text: inputtedText,
                id: currentChat._id,
                attactments: [],
                channelID: channelData._id,
            }, inputText)
        }
      }

    return <div className="content_block chat_block">

        <div className='Chat_Messages_Container'>
            {messages ?
                messages.map((message, i) =>
                    <MessageContent key={message.timestamp} message={message} memberDetails={memberDetails} i={i} total={messages.length}/>
                ) : <></>
            }
        </div>
        
        <div className='chat_textbox_container'>
            <input
                type="text"
                id="Chat_Input"
                name="Chat_Input"
                className='chat_textbox textBox'
                placeholder='Send Message'
                onChange={(e) => {inputText(e.target.value)}}
                onKeyDown={handleKeyDown}
            />

            {!hideSendButton ? <input
                type="button"
                value="↪"
                className='buttonPrimary chat_send_button'
                onClick={() => {
                    sendChat({
                        text: inputtedText,
                        id: currentChat._id,
                        attactments: [],
                        channelID: channelData._id,
                    }, inputText)
                }}
            /> : <></>}
        </div>
    </div>
}