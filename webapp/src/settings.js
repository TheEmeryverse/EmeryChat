import * as React from 'react';

   /* --------------------------------------------------------------------
     Field definition – one object per env variable.
     type can be:
        - text           (default)
        - number         (numeric input)
        - boolean        (checkbox)
        - textarea       (multiline string)
        - date           (date picker)
        - time           (time picker)
   -------------------------------------------------------------------- */
  const SETTINGS_DEFINITIONS = [
     /* General / Bot tokens ------------------------------------------------*/
     { name: 'TELEGRAM_TOKEN',            label: 'Telegram Token',            type:'text',
      placeholder: 'YOUR_TELEGRAM_TOKEN' },

     { name: 'OPEN_WEBUI_URL',            label: 'Open Web UI URL',          type:'url',
      placeholder: 'http://localhost:3000/api/v1/chat/completions' },

     { name: 'STT_URL',                   label: 'Speech‑to‑Text URL',       type:'url',
      placeholder: 'http://localhost:3000/api/v1/audio/transcriptions' },

     /* Telegram groups / topics ------------------------------------------*/
     { name: 'TELEGRAM_GROUP_CHAT_ID',    label: 'Telegram Group Chat ID',   type:'number',
      placeholder: '-100XXXXXXXXXX' },

     { name: 'SECURITY_TOPIC_ID',         label: 'Security Topic ID',        type:'number',
      placeholder: '111' },

     { name: 'ROUTINES_TOPIC_ID',         label: 'Routines Topic ID',       type:'number',
      placeholder: '222' },

     { name: 'CHAT_TOPIC_ID',             label: 'Chat Topic ID',           type:'number',
      placeholder: '333' },

     { name: 'REOLINK_SILENT_ALERTS',     label: 'Silent Alerts (default=true)', type:'boolean' },

     { name: 'TELEGRAM_ALLOWED_USERS',    label: 'Allowed User IDs (comma‑separated)', type:'textarea',
      placeholder: '123456789,987654321' },

     /* Model information ---------------------------------------------------*/
     { name: 'MODEL_NAME',                label: 'Model Name',              type:'text',
      placeholder: 'Emery' },

     { name: 'MODEL_ID',                  label: 'Text Model ID',           type:'text',
      placeholder: 'qwen3.6:35b-a3b' },

     { name: 'VISION_MODEL_ID',           label: 'Vision Model ID',          type:'text',
      placeholder: 'gemma4:e4b' },

     /* Context sizes -------------------------------------------------------*/
     { name: 'OLLAMA_NUM_CTX',            label: 'Ollama Text Context Size', type:'number',
      placeholder: '65536' },

     { name: 'OLLAMA_VISION_NUM_CTX',     label: 'Ollama Vision Context Size', type:'number',
      placeholder: '65536' },

     /* OpenAI‑like credentials --------------------------------------------*/
     { name: 'OPEN_WEBUI_KEY',            label: 'Open Web UI Key',          type:'text',
      placeholder: 'YOUR_OPEN_WEBUI_KEY' },

     /* User bio -----------------------------------------------------------*/
     { name: 'USER_NAME',                 label: 'Your Name',                type:'text',
      placeholder: 'User' },

     { name: 'USER_GENDER',               label: 'Gender',                   type:'text',
      placeholder: 'Male' },

     { name: 'USER_BIRTHDAY',             label: 'Birthday (YYYY‑MM‑DD)',   type:'date' },

     { name: 'USER_FAMILY',               label: 'Family',                   type:'textarea',
      placeholder: 'Spouse, Jane, born January 1 1990. Child, Alex, born January 1 2020.' },

     { name: 'USER_LOCATION',             label: 'Location',                 type:'text',
      placeholder: 'New York City, NY' },

     { name: 'USER_TIMEZONE',             label: 'Timezone',                 type:'text',
      placeholder: 'America/New_York' },

     /* ... you can keep adding the rest of your list here in the same pattern ... */
   ];

  const fetchUserData = async (setUserData) => {
      try {
          const response = await fetch('http://localhost:3002/user/me', { credentials: 'include' })
          const data = await response.json()
          if (data.channels) {
              setUserData({...data})
           }
       } catch {
          window.location.href = '/login'
          console.log('failed to connect to server')
       }
   }

   /* --------------------------------------------------------------------
     The Settings component
   -------------------------------------------------------------------- */
  export const Settings = () => {
    const [values, setValues] = React.useState({});
    const [loading, setLoading] = React.useState(true);
    const [msg, setMsg]      = React.useState('');
    const [userData, setUserData] = React.useState({})

    /* Fetch user data (includes configurations) ------------------------*/
    React.useEffect(() => {
        fetchUserData(setUserData)
    }, [])

    /* Populate form values from userData.configurations once loaded -----*/
    React.useEffect(() => {
        if (!userData || !userData._id) return
        const configs = userData.configurations ?? {}
        setValues(configs)
        setLoading(false)
    }, [userData])

     /* Handle input changes ---------------------------------------------*/
    const handleChange = (name, type) => e => {
      let val;
      if (type === 'boolean') {
        val = e.target.checked;
       } else if (type === 'number') {
        val = e.target.value === '' ? '' : Number(e.target.value);
       } else {
        val = e.target.value;
       }
      setValues(v => ({ ...v, [name]: val }));
     };

     /* Submit the form ----------------------------------------------*/
    const handleSubmit = async e => {
      e.preventDefault();
      try {
        const resp = await fetch('http://localhost:3002/user/configurations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify(values),
         });
        const data = await resp.json();
        if (!data.success) throw new Error(data.reason ?? 'Save failed');
        setMsg('Settings saved!');
       } catch (err) {
        console.error(err);
        setMsg(`Failed to save: ${err.message}`);
       }
     };

     /* Render -----------------------------------------------------------*/
    return (
       <div className="settings-page">
         <h2>Application Settings</h2>
         {loading ? (<p>Loading…</p>) : (
           <form onSubmit={handleSubmit}>
             {SETTINGS_DEFINITIONS.map(def => {
              const val = values[def.name] ?? '';
              return (
                 <div key={def.name} className="field-row">
                   <label htmlFor={def.name}>{def.label}</label>
                   {/* Render different input types */}
                   {def.type === 'boolean' && (
                     <input
                      id={def.name}
                      type="checkbox"
                      checked={!!val}
                      onChange={handleChange(def.name, def.type)}
                     />
                   )}
                   {(def.type === 'text' ||
                    def.type === 'number' ||
                    def.type === 'url') && (
                     <input
                      id={def.name}
                      type={def.type === 'number' ? 'number' : (def.type === 'url' ? 'url' : 'text')}
                      placeholder={def.placeholder}
                      value={val}
                      onChange={handleChange(def.name, def.type)}
                     />
                   )}
                   {def.type === 'textarea' && (
                     <textarea
                      id={def.name}
                      placeholder={def.placeholder}
                      value={val}
                      onChange={handleChange(def.name, def.type)}
                     />
                   )}
                   {(def.type === 'date' || def.type === 'time') && (
                     <input
                      id={def.name}
                      type={def.type}
                      value={val}
                      onChange={handleChange(def.name, def.type)}
                     />
                   )}
                 </div>
               );
             })}
             <button type="submit" className="buttonPrimary">Save Settings</button>
           </form>
         )}
         {msg && <p>{msg}</p>}
       </div>
     );
   };
